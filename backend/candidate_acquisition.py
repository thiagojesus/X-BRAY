"""Shared guarded selection, attempt logging, and provenance for source adapters."""

from __future__ import annotations

import json
import re
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from hashlib import sha256
from uuid import uuid4

import httpx

from candidate_eligibility import (
    APPROVED_CANDIDATE_APPROVALS,
    CandidateApproval,
    approval_blocker,
    profile_semantic_sha256,
)
from db import store
from ingestion_policy import (
    CircuitOpen,
    ErrorCategory,
    RetryBudget,
    RetryDeferred,
    classify_exception,
    current_retry_budget,
)
from source_candidates import CANDIDATE_REGISTRY, CandidateProfile
from source_ingestion import breaker_scope_id, current_extraction_id

_DIGEST = re.compile(r"^[0-9a-f]{64}$")
_SAFE_PRIMARY_FAILURES = frozenset({
    ErrorCategory.NETWORK_TIMEOUT,
    ErrorCategory.NETWORK_RESET,
    ErrorCategory.TEMPORARY_SERVER_ERROR,
    ErrorCategory.ENVELOPE_ERROR,
})


@dataclass(frozen=True, slots=True)
class AttemptMarker:
    attempt_id: str
    extraction_id: str
    source: str
    indicator: str
    candidate_id: str | None
    route: str
    request_start: int
    retry_start: int


@dataclass(frozen=True, slots=True)
class AcquiredBatch:
    observations: Sequence[Mapping[str, object]]
    payload_sha256: str
    reference_dates: tuple[str, ...]
    captured_at: str | None = None


@dataclass(frozen=True, slots=True)
class SelectionResult:
    observations: Sequence[Mapping[str, object]]
    route: str
    candidate_id: str | None
    payload_sha256: str
    reference_dates: tuple[str, ...]
    extraction_id: str
    captured_at: str
    contract_version: str | None = None
    profile_semantic_sha256: str | None = None
    parser_semantic_sha256: str | None = None
    proof_sha256: str | None = None


@dataclass(frozen=True, slots=True)
class CandidateAcquisitionService:
    """Trusted code-owned approval registry and profile resolver."""

    enabled: bool = False
    profiles: Mapping[str, CandidateProfile] = CANDIDATE_REGISTRY
    approvals: Mapping[str, CandidateApproval] = APPROVED_CANDIDATE_APPROVALS

    def resolve(
        self, candidate_id: str, contract_id: str
    ) -> tuple[CandidateProfile | None, CandidateApproval | None, str | None]:
        if not _fallback_acquisition_enabled(self.enabled):
            return None, None, "candidate_acquisition_globally_disabled"
        return approved_profile(
            candidate_id,
            contract_id,
            profiles=self.profiles,
            approvals=self.approvals,
        )


DEFAULT_CANDIDATE_ACQUISITION = CandidateAcquisitionService()


def _fallback_acquisition_enabled(injected_enabled: bool) -> bool:
    if injected_enabled:
        return True
    from source_rollout import FallbackMode, configured_mode

    mode, invalid = configured_mode()
    return mode is FallbackMode.ENABLED and not invalid


def approved_profile(
    candidate_id: str,
    contract_id: str,
    *,
    profiles: Mapping[str, CandidateProfile] = CANDIDATE_REGISTRY,
    approvals: Mapping[str, CandidateApproval] = APPROVED_CANDIDATE_APPROVALS,
) -> tuple[CandidateProfile | None, CandidateApproval | None, str | None]:
    """Resolve one exact catalog entry and fail closed on any stale proof gate."""
    profile = profiles.get(candidate_id)
    if profile is None:
        return None, None, "candidate_not_in_static_catalog"
    approval = approvals.get(candidate_id)
    blocker = approval_blocker(
        profile, approval, required_contract_id=contract_id, approvals=approvals
    )
    if blocker:
        return None, approval, blocker
    return profile, approval, None


def start_attempt(
    *,
    source: str,
    indicator: str,
    candidate_id: str | None,
    route: str,
    budget: RetryBudget,
) -> AttemptMarker:
    extraction_id = current_extraction_id()
    if extraction_id is None:
        raise RuntimeError("candidate acquisition requires an audited extraction id")
    marker = AttemptMarker(
        attempt_id=str(uuid4()),
        extraction_id=extraction_id,
        source=source,
        indicator=indicator,
        candidate_id=candidate_id,
        route=route,
        request_start=budget.requests,
        retry_start=budget.retries,
    )
    store.start_source_acquisition_attempt({
        "attempt_id": marker.attempt_id,
        "extraction_id": marker.extraction_id,
        "source": marker.source,
        "indicator": marker.indicator,
        "candidate_id": marker.candidate_id,
        "route": marker.route,
        "status": "running",
        "started_at": datetime.now(timezone.utc).isoformat(),
    })
    return marker


def finish_attempt(
    marker: AttemptMarker,
    budget: RetryBudget,
    *,
    status: str,
    payload_sha256: str | None = None,
    reference_dates: Sequence[str] = (),
    error: BaseException | None = None,
) -> None:
    if payload_sha256 is not None and _DIGEST.fullmatch(payload_sha256) is None:
        raise ValueError("invalid bounded payload digest")
    category = classify_exception(error) if error is not None else None
    store.finish_source_acquisition_attempt({
        "attempt_id": marker.attempt_id,
        "status": status,
        "finished_at": datetime.now(timezone.utc).isoformat(),
        "request_count": budget.requests - marker.request_start,
        "retry_count": budget.retries - marker.retry_start,
        "payload_sha256": payload_sha256,
        "reference_min": min(reference_dates) if reference_dates else None,
        "reference_max": max(reference_dates) if reference_dates else None,
        "error_code": type(error).__name__[:80] if error is not None else None,
        "error_category": category.value if category is not None else None,
    })


def safe_for_candidate(error: BaseException) -> bool:
    """Allow only bounded acquisition/envelope failures; never schema or quota."""
    if isinstance(error, (RetryDeferred, CircuitOpen)):
        return False
    if getattr(error, "retry_not_before", None) is not None:
        return False
    if isinstance(error, httpx.HTTPStatusError):
        return error.response.status_code >= 500
    return classify_exception(error) in _SAFE_PRIMARY_FAILURES


def record_primary_failure(error: BaseException, budget: RetryBudget, request_count: int) -> None:
    category = classify_exception(error)
    retry_at = getattr(error, "retry_not_before", None) or budget.retry_not_before
    if category not in _SAFE_PRIMARY_FAILURES or isinstance(retry_at, datetime):
        return
    now = datetime.now(timezone.utc)
    opened_until = now + timedelta(minutes=5)
    store.record_source_access_failure(
        budget.scope_id,
        now.isoformat(),
        opened_until.isoformat(),
        None,
        rate_limited=False,
        failure_count=max(1, min(request_count, 3)),
    )
    budget.primary_failure_recorded = True


def candidate_breaker_scope(profile: CandidateProfile) -> str:
    return breaker_scope_id(profile.source.value, "candidate", profile.candidate_id)


def claim_candidate_breaker(profile: CandidateProfile) -> str:
    now = datetime.now(timezone.utc)
    scope = candidate_breaker_scope(profile)
    admitted, retry_value, _half_open = store.claim_source_access_probe(
        scope,
        now.isoformat(),
        (now + timedelta(seconds=profile.deadline_seconds + 15)).isoformat(),
    )
    if not admitted:
        retry_at = now + timedelta(minutes=5)
        if retry_value:
            try:
                parsed = datetime.fromisoformat(retry_value)
                if parsed.tzinfo is None:
                    parsed = parsed.replace(tzinfo=timezone.utc)
                retry_at = max(retry_at, parsed.astimezone(timezone.utc))
            except ValueError:
                pass
        raise CircuitOpen(retry_at)
    return scope


def record_candidate_failure(
    scope: str,
    budget: RetryBudget,
    marker: AttemptMarker,
    error: BaseException,
) -> None:
    count = budget.requests - marker.request_start
    if count <= 0:
        store.release_source_access_probe(
            scope,
            datetime.now(timezone.utc).isoformat(),
            (datetime.now(timezone.utc) + timedelta(minutes=5)).isoformat(),
        )
        return
    category = classify_exception(error)
    if category in _SAFE_PRIMARY_FAILURES or category is ErrorCategory.RATE_LIMITED:
        now = datetime.now(timezone.utc)
        retry_at = getattr(error, "retry_not_before", None) or budget.retry_not_before
        if isinstance(retry_at, datetime):
            retry_at = retry_at.astimezone(timezone.utc)
        opened_until = max(now + timedelta(minutes=5), retry_at or now)
        store.record_source_access_failure(
            scope,
            now.isoformat(),
            opened_until.isoformat(),
            retry_at.isoformat() if isinstance(retry_at, datetime) else None,
            rate_limited=category is ErrorCategory.RATE_LIMITED,
            failure_count=count,
        )
    else:
        now = datetime.now(timezone.utc)
        store.release_source_access_probe(
            scope, now.isoformat(), (now + timedelta(minutes=5)).isoformat()
        )


def validate_live_references(
    approval: CandidateApproval,
    reference_dates: Sequence[str],
    *,
    stored_latest: str | None = None,
) -> None:
    distinct = sorted(set(reference_dates))
    if len(distinct) < max(10, approval.distinct_references):
        raise CandidateDataError("candidate response has insufficient distinct references")
    latest = distinct[-1]
    if latest < approval.latest_reference:
        raise CandidateDataError("candidate response predates approved proof coverage")
    if stored_latest and latest < stored_latest:
        raise CandidateDataError("candidate response is older than stored source data")


class CandidateDataError(ValueError):
    error_category = ErrorCategory.INCOMPLETE_COVERAGE


class CandidateSemanticConflict(ValueError):
    error_category = ErrorCategory.SEMANTIC_CONFLICT


def build_origin_rows(
    *,
    extraction_id: str,
    source: str,
    contract_id: str,
    route: str,
    candidate_id: str | None,
    source_payload_sha256: str,
    observations: Sequence[Mapping[str, object]],
    identity_fields: Sequence[str],
    reference_field: str,
    value_field: str,
    reference_date: Callable[[object], str] | None = None,
    captured_at: str | None = None,
    contract_version: str | None = None,
    profile_semantic_sha256: str | None = None,
    parser_semantic_sha256: str | None = None,
    proof_sha256: str | None = None,
) -> list[tuple[object, ...]]:
    if _DIGEST.fullmatch(source_payload_sha256) is None:
        raise ValueError("invalid source payload digest")
    captured_at = captured_at or datetime.now(timezone.utc).isoformat()
    result: list[tuple[object, ...]] = []
    for observation in observations:
        identity = {field: observation.get(field) for field in identity_fields}
        identity_json = json.dumps(identity, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
        value_payload = json.dumps(
            observation.get(value_field), ensure_ascii=False, sort_keys=True,
            separators=(",", ":"), allow_nan=False,
        )
        reference = observation.get(reference_field)
        reference_text = reference_date(reference) if reference_date else str(reference)
        result.append((
            str(uuid4()), extraction_id, source, contract_id, identity_json,
            reference_text, sha256(value_payload.encode("utf-8")).hexdigest(),
            route, candidate_id, source_payload_sha256,
            observation.get("source_published_at")
            if isinstance(observation.get("source_published_at"), str) else None,
            captured_at,
            contract_version,
            profile_semantic_sha256,
            parser_semantic_sha256,
            proof_sha256,
        ))
    return result


def commit_receipt(
    extraction_id: str,
    *,
    route: str,
    candidate_id: str | None,
    observation_count: int,
    source_payload_sha256: str,
) -> tuple[object, ...]:
    if _DIGEST.fullmatch(source_payload_sha256) is None or observation_count < 0:
        raise ValueError("invalid source acquisition receipt")
    return (
        extraction_id, route, candidate_id, observation_count,
        source_payload_sha256, datetime.now(timezone.utc).isoformat(),
    )


def has_commit_receipt(extraction_id: str) -> bool:
    return store.source_acquisition_commit(extraction_id) is not None


def commit_receipt_matches(
    selection: SelectionResult, *, reconnect: bool = False
) -> bool:
    receipt = store.source_acquisition_commit(
        selection.extraction_id, reconnect=reconnect
    )
    if receipt is None:
        return False
    count = receipt.get("observation_count")
    return (
        receipt.get("selected_route") == selection.route
        and receipt.get("candidate_id") == selection.candidate_id
        and isinstance(count, int)
        and count == len(selection.observations)
        and receipt.get("source_payload_sha256") == selection.payload_sha256
    )


def active_budget() -> RetryBudget:
    budget = current_retry_budget()
    if budget is None:
        raise RuntimeError("candidate acquisition requires a shared source request budget")
    return budget


def select_and_persist(
    *,
    profile: CandidateProfile,
    approval: CandidateApproval,
    indicator: str,
    contract_id: str,
    acquisition_unit_id: str,
    primary_acquire: Callable[[float], AcquiredBatch],
    candidate_acquire: Callable[[float], AcquiredBatch],
    stored_latest: Callable[[], str | None],
    persist: Callable[[SelectionResult], None],
    unit_deadline_seconds: float,
    service: CandidateAcquisitionService = DEFAULT_CANDIDATE_ACQUISITION,
) -> SelectionResult:
    """Acquire a primary batch first; use one qualified alternate only on safe failure."""
    from source_ingestion import SourceOperationError, acquisition_unit

    if not _fallback_acquisition_enabled(service.enabled):
        raise ValueError("candidate acquisition is globally disabled")
    catalog_profile = service.profiles.get(profile.candidate_id)
    if catalog_profile is None:
        raise ValueError("candidate is absent from the static catalog")
    if profile != catalog_profile:
        raise ValueError("candidate profile differs from the static catalog")
    approvals = service.approvals
    reviewed_approval = approvals.get(profile.candidate_id)
    if reviewed_approval != approval:
        raise ValueError("candidate approval is not in the static approval registry")
    blocker = approval_blocker(
        profile,
        reviewed_approval,
        required_contract_id=contract_id,
        approvals=approvals,
    )
    if blocker is not None:
        raise ValueError(f"candidate is not eligible: {blocker}")

    budget = active_budget()
    extraction_id = current_extraction_id()
    if extraction_id is None:
        raise RuntimeError("candidate selection requires an audited extraction id")
    primary_errors: list[BaseException] = []
    primary_request_start = budget.requests
    primary_circuit_open = False
    primary_batch: AcquiredBatch | None = None
    unit_token = None
    try:
        unit_context = acquisition_unit(
            acquisition_unit_id, 3, min(unit_deadline_seconds, profile.deadline_seconds)
        )
        unit_context.__enter__()
        unit_token = unit_context
        while primary_batch is None and not primary_errors:
            marker = start_attempt(
                source=profile.source.value,
                indicator=indicator,
                candidate_id=None,
                route="primary",
                budget=budget,
            )
            remaining = budget.remaining_seconds()
            reserve = min(5.0, profile.deadline_seconds / 4)
            primary_timeout = min(
                profile.deadline_seconds,
                max(0.0, remaining - reserve),
            )
            if primary_timeout < 1.0:
                error = RetryDeferred(
                    datetime.now(timezone.utc) + timedelta(seconds=1),
                    "primary attempt cannot preserve candidate request time",
                )
                finish_attempt(marker, budget, status="deferred", error=error)
                primary_errors.append(error)
                break
            try:
                route_token = budget.enter_request_group("primary")
                try:
                    primary_batch = primary_acquire(primary_timeout)
                finally:
                    budget.leave_request_group(route_token)
                if not primary_batch.observations:
                    raise CandidateDataError("primary source returned an empty cohort")
                finish_attempt(
                    marker,
                    budget,
                    status="success",
                    payload_sha256=primary_batch.payload_sha256,
                    reference_dates=primary_batch.reference_dates,
                )
                break
            except Exception as error:
                finish_attempt(
                    marker,
                    budget,
                    status="network_error" if safe_for_candidate(error) else _attempt_status(error),
                    error=error,
                )
                if isinstance(error, CircuitOpen):
                    primary_circuit_open = True
                    primary_errors.append(error)
                    break
                if not safe_for_candidate(error):
                    raise
                primary_errors.append(error)
                if budget.remaining_seconds() < 2:
                    break

        if primary_batch is not None:
            selection = SelectionResult(
                primary_batch.observations, "primary", None, primary_batch.payload_sha256,
                primary_batch.reference_dates, extraction_id,
                primary_batch.captured_at or datetime.now(timezone.utc).isoformat(),
            )
            try:
                persist(selection)
            except Exception as error:
                if commit_receipt_matches(selection, reconnect=True):
                    return selection
                raise SourceOperationError(
                    "selected source batch could not be committed",
                    stage="persistence",
                    error_category=ErrorCategory.PERSISTENCE_FAILURE,
                ) from error
            return selection

        last_primary_error = primary_errors[-1] if primary_errors else TimeoutError("primary source did not produce a batch")
        if not primary_errors or not (
            safe_for_candidate(last_primary_error) or primary_circuit_open
        ):
            raise last_primary_error
        # The shared budget may include other datasets; isolate this primary
        # operation's outbound requests for its breaker accounting.
        primary_request_count = min(3, budget.requests - primary_request_start)
        if not primary_circuit_open:
            record_primary_failure(last_primary_error, budget, max(1, primary_request_count))

        candidate_marker = start_attempt(
            source=profile.source.value,
            indicator=indicator,
            candidate_id=profile.candidate_id,
            route="candidate",
            budget=budget,
        )
        original_request_gate = budget.request_gate
        candidate_scope: str | None = None
        try:
            if primary_circuit_open:
                root_state = store.get_source_access_state(budget.scope_id)
                if root_state is None or root_state.get("retry_not_before") is not None:
                    raise RetryDeferred(
                        datetime.now(timezone.utc) + timedelta(minutes=5),
                        "primary publisher quota/cooldown blocks candidate acquisition",
                    )
                # A transient primary circuit may be bypassed only for this
                # independently guarded candidate endpoint. Its request still
                # consumes the same root budget and deadline.
                budget.request_gate = None
            candidate_scope = claim_candidate_breaker(profile)
            remaining = budget.remaining_seconds()
            if remaining < 1.0:
                raise RetryDeferred(
                    datetime.now(timezone.utc) + timedelta(seconds=1),
                    "no request budget remains for the approved candidate",
                )
            route_token = budget.enter_request_group("candidate")
            try:
                candidate_batch = candidate_acquire(min(profile.deadline_seconds, remaining))
            finally:
                budget.leave_request_group(route_token)
            validate_live_references(
                approval,
                candidate_batch.reference_dates,
                stored_latest=stored_latest(),
            )
            if not candidate_batch.observations:
                raise CandidateDataError("candidate response has an empty cohort")
            store.record_source_access_success(
                candidate_scope, datetime.now(timezone.utc).isoformat()
            )
            selection = SelectionResult(
                candidate_batch.observations,
                "candidate",
                profile.candidate_id,
                candidate_batch.payload_sha256,
                candidate_batch.reference_dates,
                extraction_id,
                candidate_batch.captured_at or datetime.now(timezone.utc).isoformat(),
                profile.source_contract_version,
                profile_semantic_sha256(profile),
                approval.parser_semantic_sha256,
                approval.proof_sha256,
            )
            try:
                persist(selection)
            except Exception as error:
                if commit_receipt_matches(selection, reconnect=True):
                    finish_attempt(
                        candidate_marker,
                        budget,
                        status="success",
                        payload_sha256=candidate_batch.payload_sha256,
                        reference_dates=candidate_batch.reference_dates,
                    )
                    return selection
                finish_attempt(
                    candidate_marker,
                    budget,
                    status="persistence_error",
                    payload_sha256=candidate_batch.payload_sha256,
                    reference_dates=candidate_batch.reference_dates,
                    error=error,
                )
                raise SourceOperationError(
                    "selected candidate batch could not be committed",
                    stage="persistence",
                    error_category=ErrorCategory.PERSISTENCE_FAILURE,
                    preserve_primary_breaker=True,
                ) from error
            finish_attempt(
                candidate_marker,
                budget,
                status="success",
                payload_sha256=candidate_batch.payload_sha256,
                reference_dates=candidate_batch.reference_dates,
            )
            return selection
        except Exception as error:
            record_candidate_failure(
                candidate_scope or candidate_breaker_scope(profile),
                budget,
                candidate_marker,
                error,
            )
            finish_attempt(
                candidate_marker,
                budget,
                status=_attempt_status(error),
                error=error,
            )
            category = classify_exception(error)
            stage = "network" if category in _SAFE_PRIMARY_FAILURES or category in {
                ErrorCategory.RATE_LIMITED, ErrorCategory.DEFERRED, ErrorCategory.CIRCUIT_OPEN,
            } else "validation"
            retry_not_before = getattr(error, "retry_not_before", None)
            raise SourceOperationError(
                "approved candidate could not complete the acquisition unit",
                stage=stage,
                error_category=category,
                retry_not_before=retry_not_before.isoformat()
                if isinstance(retry_not_before, datetime) else None,
                preserve_primary_breaker=True,
            ) from error
        finally:
            budget.request_gate = original_request_gate
    finally:
        if unit_token is not None:
            unit_token.__exit__(None, None, None)


def _attempt_status(error: BaseException) -> str:
    category = classify_exception(error)
    if category in {ErrorCategory.DEFERRED, ErrorCategory.CIRCUIT_OPEN, ErrorCategory.RATE_LIMITED}:
        return "deferred"
    if category in {ErrorCategory.INVALID_SCHEMA, ErrorCategory.INCOMPLETE_COVERAGE}:
        return "validation_error"
    if category is ErrorCategory.SEMANTIC_CONFLICT:
        return "semantic_conflict"
    if category is ErrorCategory.PERSISTENCE_FAILURE:
        return "persistence_error"
    if category in _SAFE_PRIMARY_FAILURES:
        return "network_error"
    return "internal_error"
