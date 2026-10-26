"""Authenticated rollout diagnostics and fail-closed manual canary control."""

from __future__ import annotations

import re
import tempfile
from datetime import date, datetime, timezone
from pathlib import Path
from typing import Annotated, cast

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, ConfigDict, Field, StrictBool, StrictStr

from canary_executor import (
    CanaryCommitOutcome,
    CanaryExecutionError,
    binding_matches_request,
    execute_canary_commit,
    known_receipt_outcome,
    make_request_binding,
)
from candidate_eligibility import APPROVED_CANDIDATE_APPROVALS
from db import store
from refresh_auth import require_refresh_auth
from source_candidates import CANDIDATE_REGISTRY, get_candidate_profile
from source_rollout import FallbackMode, configured_mode, readiness_snapshot

router = APIRouter(prefix="/api/source-candidates", tags=["source-candidates"])
_SHA256 = re.compile(r"^[0-9a-f]{64}$")
_OPERATION_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.:-]{0,127}$")


class CanaryRequest(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    candidate_id: Annotated[StrictStr, Field(min_length=1, max_length=100)]
    as_of: date | None = None
    dry_run: StrictBool = True
    confirm_write: StrictBool = False
    expected_proof_sha256: StrictStr | None = None
    expected_contract_version: StrictStr | None = None
    operation_id: Annotated[StrictStr, Field(min_length=1, max_length=128)] | None = None


def _mode_snapshot() -> tuple[FallbackMode, bool, dict[str, object]]:
    mode, invalid = configured_mode()
    states = store.source_access_states()
    snapshot = readiness_snapshot(
        mode=mode,
        mode_configuration_error=invalid,
        access_states=states,
    )
    return mode, invalid, snapshot


def _candidate_activity(
    candidate_id: str,
    profile_source: str,
    indicator: str,
    activity: dict[str, list[dict[str, object]]],
) -> dict[str, object]:
    extractions = activity["extractions"]
    matching = [
        item for item in extractions
        if item.get("source") == profile_source and item.get("indicator") == indicator
    ]
    attempts = activity["attempts"]
    scoped_attempts = [
        item for item in attempts
        if item.get("source") == profile_source and item.get("indicator") == indicator
    ]
    origins = [item for item in activity["origins"] if item.get("candidate_id") == candidate_id]
    latest = matching[0] if matching else None
    primary_attempt = next((item for item in scoped_attempts if item.get("route") == "primary"), None)
    candidate_attempt = next((item for item in scoped_attempts if item.get("route") == "candidate"), None)
    return {
        "latest_operation": _safe_operation(latest),
        "latest_primary_attempt": _safe_attempt(primary_attempt),
        "latest_candidate_attempt": _safe_attempt(candidate_attempt),
        "candidate_origin_rows_seen": len(origins),
        "latest_candidate_origin_capture": origins[0].get("captured_at") if origins else None,
    }


def _safe_operation(row: dict[str, object] | None) -> dict[str, object] | None:
    if row is None:
        return None
    keys = (
        "status", "started_at", "finished_at", "network_called", "source_data_date",
        "source_reference_min", "source_reference_max", "request_count", "retry_count",
        "error_code", "error_category", "retry_not_before", "selected_route",
        "candidate_id", "observation_count", "source_payload_sha256", "committed_at",
    )
    return {key: row.get(key) for key in keys}


def _safe_attempt(row: dict[str, object] | None) -> dict[str, object] | None:
    if row is None:
        return None
    keys = (
        "route", "status", "started_at", "finished_at", "request_count", "retry_count",
        "error_code", "error_category", "reference_min", "reference_max",
    )
    return {key: row.get(key) for key in keys}


def _canary_payload(
    outcome: CanaryCommitOutcome,
    *,
    as_of: date,
    dry_run: bool,
) -> dict[str, object]:
    return {
        "schema_version": "source-canary-result-v1",
        "status": outcome.status,
        "candidate_id": outcome.candidate_id,
        "operation_id": outcome.operation_id,
        "as_of": as_of.isoformat(),
        "reference_period": outcome.reference_period,
        "dry_run": dry_run,
        "network_called": outcome.request_count > 0,
        "request_count": outcome.request_count,
        "committed": outcome.committed,
        "selected_route": "candidate" if outcome.committed else None,
        "selected_observations": outcome.selected_observations,
        "business_writes": outcome.business_writes,
        "inserted_count": outcome.inserted_count,
        "updated_count": outcome.updated_count,
        "origin_count": outcome.origin_count,
        "receipt_count": outcome.receipt_count,
        "captured_at": outcome.captured_at,
        "full_refresh_watermarks_updated": False,
    }


@router.get("/readiness", dependencies=[Depends(require_refresh_auth)])
def source_candidate_readiness() -> dict[str, object]:
    """Return static proof, mode, and breaker readiness without touching source APIs."""
    _mode, _invalid, snapshot = _mode_snapshot()
    candidates = snapshot["candidates"]
    if not isinstance(candidates, list):
        raise TypeError("invalid candidate readiness snapshot")
    activity = store.source_candidate_activity(200)
    for item in cast(list[dict[str, object]], candidates):
        candidate_id = item.get("candidate_id")
        if not isinstance(candidate_id, str):
            continue
        profile = CANDIDATE_REGISTRY[candidate_id]
        indicator = profile.source_contract_ids[0].removeprefix("sgs:").removeprefix("sidra:")
        item["activity"] = _candidate_activity(
            candidate_id,
            profile.source.value,
            indicator,
            activity,
        )
    return snapshot


@router.post("/canary", dependencies=[Depends(require_refresh_auth)])
def source_candidate_canary(request: CanaryRequest) -> dict[str, object]:
    """Dry-run is read-only; a write request must pass every static gate first."""
    try:
        profile = get_candidate_profile(request.candidate_id)
    except (KeyError, ValueError):
        raise HTTPException(status_code=404, detail="unknown_candidate") from None
    today = datetime.now(timezone.utc).date()
    as_of = request.as_of or today
    if as_of > today or (today - as_of).days > 60:
        raise HTTPException(status_code=422, detail="canary_as_of_out_of_range")
    mode, mode_error, snapshot = _mode_snapshot()
    rows = cast(list[dict[str, object]], snapshot["candidates"])
    candidate_row = next(
        row for row in rows
        if row.get("candidate_id") == profile.candidate_id
    )
    readiness = dict(candidate_row)
    readiness["activity"] = _candidate_activity(
        profile.candidate_id,
        profile.source.value,
        profile.source_contract_ids[0].removeprefix("sgs:").removeprefix("sidra:"),
        store.source_candidate_activity(200),
    )
    if request.dry_run:
        if mode is FallbackMode.SHADOW and profile.adapter is not None:
            from shadow_ingestion import run_shadow

            with tempfile.TemporaryDirectory(prefix="xbray-source-shadow-") as directory:
                artifact = run_shadow(
                    profile.candidate_id,
                    as_of,
                    Path(directory) / "evidence.json",
                )
            evidence = artifact.get("evidence")
            evidence_info = cast(dict[str, object], evidence) if isinstance(evidence, dict) else {}
            network = artifact.get("network")
            network_info = cast(dict[str, object], network) if isinstance(network, dict) else {}
            return {
                "schema_version": "source-canary-result-v1",
                "status": artifact.get("status", "acquisition_failed"),
                "candidate_id": profile.candidate_id,
                "as_of": as_of.isoformat(),
                "dry_run": True,
                "network_called": network_info.get("actual_outbound_requests", 0),
                "business_writes": 0,
                "freshness_metadata_writes": 0,
                "proof_sha256": artifact.get("candidate_profile_sha256"),
                "source_contract_version": artifact.get("source_contract_version"),
                "evidence_status": evidence_info.get("evidence_status"),
                "eligibility_blockers": artifact.get("eligibility_blockers", []),
                "readiness": readiness,
            }
        return {
            "schema_version": "source-canary-result-v1",
            "status": "blocked" if readiness.get("ready") is not True else "preflight_only",
            "candidate_id": profile.candidate_id,
            "as_of": as_of.isoformat(),
            "dry_run": True,
            "network_called": False,
            "business_writes": 0,
            "freshness_metadata_writes": 0,
            "readiness": readiness,
            "detail": "read-only preflight; no provider transport is performed by this control endpoint",
        }

    if not request.confirm_write:
        raise HTTPException(status_code=422, detail="canary_write_confirmation_required")
    if mode is not FallbackMode.CANARY or mode_error:
        raise HTTPException(status_code=409, detail="canary_mode_not_enabled")
    approval = APPROVED_CANDIDATE_APPROVALS.get(profile.candidate_id)
    expected_hash = request.expected_proof_sha256
    if (
        approval is None
        or expected_hash is None
        or _SHA256.fullmatch(expected_hash) is None
        or expected_hash != approval.proof_sha256
        or request.expected_contract_version != profile.source_contract_version
        or not request.operation_id
    ):
        raise HTTPException(status_code=409, detail="candidate_approval_confirmation_mismatch")
    if readiness.get("ready") is not True:
        raise HTTPException(status_code=409, detail="candidate_not_ready")

    if _OPERATION_ID.fullmatch(request.operation_id) is None:
        raise HTTPException(status_code=422, detail="invalid_canary_operation_id")
    try:
        request_binding = make_request_binding(profile, as_of)
    except (ValueError, KeyError):
        raise HTTPException(status_code=409, detail="candidate_not_supported_for_canary") from None

    with store.canary_operation_lock(request.operation_id):
        durable_binding = store.source_canary_binding(request.operation_id)
        if durable_binding is not None:
            if not binding_matches_request(
                durable_binding,
                profile=profile,
                approval=approval,
                request_binding=request_binding,
                operation_id=request.operation_id,
                as_of=as_of,
            ):
                raise HTTPException(status_code=409, detail="canary_operation_binding_conflict")
            try:
                outcome = known_receipt_outcome(
                    durable_binding,
                    store.source_acquisition_commit(request.operation_id),
                )
            except RuntimeError:
                raise HTTPException(status_code=409, detail="canary_operation_receipt_inconsistent") from None
            return _canary_payload(outcome, as_of=as_of, dry_run=False)

        try:
            outcome = execute_canary_commit(
                profile,
                approval,
                as_of,
                request.operation_id,
                request_binding,
            )
        except CanaryExecutionError as error:
            from canary_acquisition import CanaryAcquisitionError
            from ingestion_policy import ErrorCategory

            cause = error.__cause__
            if isinstance(cause, CanaryAcquisitionError) and cause.error_category is ErrorCategory.SEMANTIC_CONFLICT:
                raise HTTPException(status_code=409, detail="primary_candidate_comparison_failed") from None
            if isinstance(cause, CanaryAcquisitionError) and cause.error_category in {
                ErrorCategory.RATE_LIMITED,
                ErrorCategory.DEFERRED,
                ErrorCategory.CIRCUIT_OPEN,
            }:
                raise HTTPException(status_code=429, detail="canary_provider_budget_deferred") from None
            if getattr(cause, "stage", None) == "logging":
                raise HTTPException(status_code=500, detail="canary_committed_reporting_failed") from None
            raise HTTPException(status_code=502, detail="canary_acquisition_or_persistence_failed") from None
        return _canary_payload(outcome, as_of=as_of, dry_run=False)
