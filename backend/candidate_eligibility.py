"""Fail-closed proof binding for statically reviewed ingestion alternatives."""

from __future__ import annotations

import inspect
import json
import re
from collections.abc import Mapping
from dataclasses import asdict, dataclass
from hashlib import sha256
from types import MappingProxyType

from source_candidates import CandidateProfile, CandidateStatus, ShadowAdapter

_SHA256 = re.compile(r"^[0-9a-f]{64}$")


@dataclass(frozen=True, slots=True)
class CandidateApproval:
    candidate_id: str
    contract_version: str
    profile_semantic_sha256: str
    parser_semantic_sha256: str
    proof_sha256: str
    distinct_references: int
    latest_reference: str
    code_reviewed: bool
    license_verified: bool
    complete_cohort_proven: bool = False


# Production remains primary-only until a reviewed, rights-verified approval is
# added in code. Request parameters and CLI/artifact data cannot populate this map.
APPROVED_CANDIDATE_APPROVALS: Mapping[str, CandidateApproval] = MappingProxyType({})


def profile_semantic_sha256(profile: CandidateProfile) -> str:
    """Hash source semantics while excluding activation and evidence-gate state."""
    payload = asdict(profile)
    for key in (
        "status", "enabled_for_ingestion", "disabled_reason", "license_status",
        "license_evidence_url", "approved_proof_sha256", "code_reviewed", "priority",
    ):
        payload.pop(key, None)
    payload["source"] = profile.source.value
    payload["adapter"] = profile.adapter.value if profile.adapter else None
    return sha256(json.dumps(payload, sort_keys=True, separators=(",", ":")).encode()).hexdigest()


def parser_semantic_sha256(candidate_id: str) -> str:
    """Fingerprint the actual producer/alternate decoders used for selection."""
    import shadow_ingestion
    from datafetchers import bcb_sgs, ibge_sidra

    if candidate_id.startswith("bcb_sgs:csv:"):
        function_specs = (
            (bcb_sgs, "_validate_sgs_payload"),
            (bcb_sgs, "_normalize_window_records"),
            (shadow_ingestion, "parse_sgs_csv"),
            (bcb_sgs, "_candidate_sgs_records"),
            (bcb_sgs, "_sgs_acquired_batch"),
        )
    elif candidate_id.startswith("ibge_sidra:v3:"):
        function_specs = (
            (ibge_sidra, "_parse_row"),
            (ibge_sidra, "parse_sidra_payload"),
            (shadow_ingestion, "_sidra_v1_primary"),
            (shadow_ingestion, "_sidra_category"),
            (shadow_ingestion, "_sidra_v3_series_rows"),
            (shadow_ingestion, "_sidra_observations"),
            (shadow_ingestion, "parse_sidra_v3"),
            (ibge_sidra, "_sidra_as_of"),
            (ibge_sidra, "_sidra_candidate_observations"),
            (ibge_sidra, "_sidra_reference_label"),
            (ibge_sidra, "_sidra_acquired_batch"),
        )
    else:
        return ""
    functions = tuple(getattr(module, name) for module, name in function_specs)
    material = "\n\n".join(inspect.getsource(function) for function in functions)
    return sha256(material.encode("utf-8")).hexdigest()


def approval_blocker(
    profile: CandidateProfile,
    approval: CandidateApproval | None,
    *,
    required_contract_id: str,
    approvals: Mapping[str, CandidateApproval] = APPROVED_CANDIDATE_APPROVALS,
) -> str | None:
    """Return a stable reason unless every reviewed static gate matches."""
    selected = approval if approval is not None else approvals.get(profile.candidate_id)
    if selected is None:
        return "no_static_candidate_approval"
    if profile.candidate_id != selected.candidate_id:
        return "candidate_id_mismatch"
    if profile.status is not CandidateStatus.ELIGIBLE or not profile.enabled_for_ingestion:
        return "candidate_ingestion_disabled"
    if profile.adapter not in {ShadowAdapter.SGS_CSV, ShadowAdapter.SIDRA_V3}:
        return "candidate_adapter_unsupported"
    if required_contract_id not in profile.source_contract_ids:
        return "source_contract_mismatch"
    if profile.source_contract_version != selected.contract_version:
        return "source_contract_version_stale"
    if profile_semantic_sha256(profile) != selected.profile_semantic_sha256:
        return "candidate_profile_semantics_changed"
    if parser_semantic_sha256(profile.candidate_id) != selected.parser_semantic_sha256:
        return "candidate_parser_semantics_changed"
    if (
        not profile.code_reviewed
        or not selected.code_reviewed
        or not profile.approved_proof_sha256
        or profile.approved_proof_sha256 != selected.proof_sha256
        or _SHA256.fullmatch(selected.proof_sha256) is None
    ):
        return "reviewed_proof_not_bound"
    if profile.license_status != "verified" or not selected.license_verified:
        return "license_evidence_unverified"
    if selected.distinct_references < 10:
        return "proof_has_fewer_than_10_references"
    if not selected.latest_reference:
        return "proof_latest_reference_missing"
    if not selected.complete_cohort_proven:
        return "complete_cohort_not_proven"
    return None
