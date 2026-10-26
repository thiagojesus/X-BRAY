"""Read-only configuration and readiness gates for future source rollout."""

from __future__ import annotations

import os
import re
from collections.abc import Mapping
from datetime import datetime, timezone
from enum import StrEnum
from typing import Final

from candidate_eligibility import (
    APPROVED_CANDIDATE_APPROVALS,
    CandidateApproval,
    approval_blocker,
    profile_semantic_sha256,
)
from source_candidates import CANDIDATE_REGISTRY, CandidateProfile

_SHA256: Final = re.compile(r"^[0-9a-f]{64}$")


class FallbackMode(StrEnum):
    OFF = "off"
    SHADOW = "shadow"
    CANARY = "canary"
    ENABLED = "enabled"


def configured_mode() -> tuple[FallbackMode, bool]:
    """Return the env-selected mode and whether configuration parsed cleanly."""
    raw = os.environ.get("SOURCE_FALLBACK_MODE", FallbackMode.OFF.value)
    try:
        return FallbackMode(raw.strip().lower()), False
    except ValueError:
        return FallbackMode.OFF, True


def candidate_readiness(
    profile: CandidateProfile,
    *,
    mode: FallbackMode,
    mode_configuration_error: bool,
    approvals: Mapping[str, CandidateApproval] = APPROVED_CANDIDATE_APPROVALS,
    primary_state: Mapping[str, object] | None = None,
    candidate_state: Mapping[str, object] | None = None,
) -> dict[str, object]:
    """Explain every static and operational blocker without acquiring a probe lease."""
    approval = approvals.get(profile.candidate_id)
    contract_id = profile.source_contract_ids[0] if profile.source_contract_ids else ""
    blocker = approval_blocker(
        profile,
        approval,
        required_contract_id=contract_id,
        approvals=approvals,
    )
    evidence_hash = getattr(approval, "proof_sha256", None)
    reference_count = getattr(approval, "distinct_references", 0)
    latest_reference = getattr(approval, "latest_reference", None)
    license_verified = bool(getattr(approval, "license_verified", False))
    parser_supported = profile.adapter is not None
    evidence_valid = (
        isinstance(evidence_hash, str)
        and _SHA256.fullmatch(evidence_hash) is not None
        and isinstance(reference_count, int)
        and not isinstance(reference_count, bool)
        and reference_count >= 10
        and isinstance(latest_reference, str)
        and bool(latest_reference)
    )
    blockers: list[str] = []
    if mode_configuration_error:
        blockers.append("fallback_mode_invalid_defaulted_off")
    if mode is FallbackMode.OFF:
        blockers.append("fallback_mode_off")
    if not parser_supported:
        blockers.append("candidate_parser_unsupported")
    if profile.official_evidence_url is None:
        blockers.append("official_source_evidence_missing")
    if not license_verified or profile.license_status != "verified":
        blockers.append("license_evidence_unverified")
    if approval is None:
        blockers.append("no_static_candidate_approval")
    elif not evidence_valid:
        blockers.append("proof_evidence_incomplete")
    if blocker and blocker not in blockers:
        blockers.append(blocker)
    if not profile.enabled_for_ingestion:
        blockers.append("candidate_ingestion_disabled")
    if not profile.code_reviewed:
        blockers.append("candidate_code_unreviewed")
    # The canary acquires primary and candidate independently. An ordinary
    # primary circuit breaker must not permanently prohibit this diagnostic;
    # candidate access (including shared quota cooldown) still gates it.
    if _is_cooling(candidate_state):
        blockers.append("candidate_circuit_or_quota_cooldown")
    return {
        "candidate_id": profile.candidate_id,
        "source": profile.source.value,
        "source_contract_ids": list(profile.source_contract_ids),
        "source_contract_version": profile.source_contract_version,
        "candidate_code_version": profile.code_version,
        "semantic_profile_sha256": profile_semantic_sha256(profile),
        "parser_supported": parser_supported,
        "official_evidence_available": profile.official_evidence_url is not None,
        "official_evidence_url": profile.official_evidence_url,
        "license_status": profile.license_status,
        "license_evidence_available": profile.license_evidence_url is not None,
        "evidence_sha256": evidence_hash if isinstance(evidence_hash, str) else None,
        "evidence_valid": evidence_valid,
        "distinct_references": reference_count if isinstance(reference_count, int) else 0,
        "latest_reference": latest_reference if isinstance(latest_reference, str) else None,
        "candidate_enabled": profile.enabled_for_ingestion,
        "code_reviewed": profile.code_reviewed,
        "mode": mode.value,
        "primary_access": _state_summary(primary_state),
        "candidate_access": _state_summary(candidate_state),
        "ready": not blockers,
        "blockers": list(dict.fromkeys(blockers)),
        "checked_at": datetime.now(timezone.utc).isoformat(),
    }


def _is_cooling(state: Mapping[str, object] | None) -> bool:
    if state is None:
        return False
    retry_at = state.get("retry_not_before")
    if retry_at is not None:
        try:
            parsed = datetime.fromisoformat(str(retry_at).replace("Z", "+00:00"))
            if parsed.tzinfo is not None and parsed <= datetime.now(timezone.utc):
                retry_at = None
        except ValueError:
            pass
    return state.get("state") in {"open", "half_open"} and retry_at is not None or retry_at is not None


def _state_summary(state: Mapping[str, object] | None) -> dict[str, object]:
    if state is None:
        return {"state": "unknown", "retry_not_before": None, "probe_lease_active": False}
    retry_at = state.get("retry_not_before")
    return {
        "state": state.get("state", "unknown"),
        "retry_not_before": retry_at,
        "probe_lease_active": bool(state.get("probe_lease_until")),
    }


def readiness_snapshot(
    *,
    mode: FallbackMode,
    mode_configuration_error: bool,
    access_states: Mapping[str, Mapping[str, object]],
) -> dict[str, object]:
    """Build a bounded per-profile operator view with no source requests or writes."""
    candidates: list[dict[str, object]] = []
    for profile in CANDIDATE_REGISTRY.values():
        primary_key = f"{profile.source.value}:primary:primary"
        candidate_key = f"{profile.source.value}:candidate:{profile.candidate_id}"
        candidates.append(candidate_readiness(
            profile,
            mode=mode,
            mode_configuration_error=mode_configuration_error,
            primary_state=access_states.get(primary_key),
            candidate_state=access_states.get(candidate_key),
        ))
    ready_count = sum(item["ready"] is True for item in candidates)
    return {
        "schema_version": "source-candidate-readiness-v1",
        "mode": mode.value,
        "mode_configuration_error": mode_configuration_error,
        "global_acquisition_enabled": mode is FallbackMode.ENABLED,
        "production_approvals_count": len(APPROVED_CANDIDATE_APPROVALS),
        "candidate_count": len(candidates),
        "ready_count": ready_count,
        "candidates": candidates,
    }
