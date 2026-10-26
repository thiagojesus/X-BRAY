from dataclasses import replace

import pytest

from candidate_eligibility import (
    CandidateApproval,
    approval_blocker,
    parser_semantic_sha256,
    profile_semantic_sha256,
)
from source_candidates import CandidateStatus, get_candidate_profile


def test_production_registry_is_primary_only_for_unverified_sidra_profile():
    profile = get_candidate_profile("ibge_sidra:v3:pim_industria")

    assert profile.status is CandidateStatus.SHADOW
    assert not profile.enabled_for_ingestion
    assert approval_blocker(
        profile, None, required_contract_id="sidra:pim_industria"
    ) == "no_static_candidate_approval"


def test_profile_semantic_hash_ignores_activation_but_binds_endpoint():
    profile = get_candidate_profile("ibge_sidra:v3:pim_industria")
    activated = replace(
        profile,
        status=CandidateStatus.ELIGIBLE,
        enabled_for_ingestion=True,
        code_reviewed=True,
        approved_proof_sha256="a" * 64,
        license_status="verified",
    )

    assert profile_semantic_sha256(profile) == profile_semantic_sha256(activated)
    assert profile_semantic_sha256(profile) != profile_semantic_sha256(
        replace(profile, endpoint_path_template="/changed/{table_id}")
    )


@pytest.mark.parametrize("license_status", ["unverified", "unknown", "verified"])
def test_approval_requires_code_bound_evidence_and_verified_license(license_status: str):
    profile = get_candidate_profile("ibge_sidra:v3:pim_industria")
    proof = "b" * 64
    approved = replace(
        profile,
        status=CandidateStatus.ELIGIBLE,
        enabled_for_ingestion=True,
        code_reviewed=True,
        approved_proof_sha256=proof,
        license_status=license_status,
    )
    approval = CandidateApproval(
        candidate_id=approved.candidate_id,
        contract_version=approved.source_contract_version,
        profile_semantic_sha256=profile_semantic_sha256(approved),
        parser_semantic_sha256=parser_semantic_sha256(approved.candidate_id),
        proof_sha256=proof,
        distinct_references=10,
        latest_reference="2026-08-01",
        code_reviewed=True,
        license_verified=True,
        complete_cohort_proven=True,
    )

    expected = None if license_status == "verified" else "license_evidence_unverified"
    assert approval_blocker(
        approved,
        approval,
        required_contract_id="sidra:pim_industria",
        approvals={approved.candidate_id: approval},
    ) == expected
