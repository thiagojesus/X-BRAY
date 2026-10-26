from __future__ import annotations

from dataclasses import FrozenInstanceError

import pytest

from source_candidates import (
    CANDIDATE_REGISTRY,
    CandidateStatus,
    ShadowAdapter,
    get_candidate_profile,
)
from source_contracts import SGS_CONTRACTS, SIDRA_CONTRACTS, Source


def test_candidate_registry_is_immutable_and_every_source_has_an_explicit_descriptor() -> None:
    sources = {profile.source for profile in CANDIDATE_REGISTRY.values()}
    assert sources == set(Source)
    assert len([key for key in CANDIDATE_REGISTRY if key.startswith("bcb_sgs:csv:")]) == len(SGS_CONTRACTS)
    assert len([key for key in CANDIDATE_REGISTRY if key.startswith("ibge_sidra:v3:")]) == len(SIDRA_CONTRACTS)
    assert all(not profile.enabled_for_ingestion for profile in CANDIDATE_REGISTRY.values())
    assert all(profile.status is not CandidateStatus.ELIGIBLE for profile in CANDIDATE_REGISTRY.values())
    with pytest.raises(TypeError):
        CANDIDATE_REGISTRY["attacker:url"] = get_candidate_profile("bcb_sgs:csv:433")
    with pytest.raises(FrozenInstanceError):
        get_candidate_profile("bcb_sgs:csv:433").endpoint_host = "attacker.invalid"


def test_only_two_fixed_official_protocols_have_shadow_adapters() -> None:
    active_adapters = {
        profile.adapter
        for profile in CANDIDATE_REGISTRY.values()
        if profile.adapter is not None
    }
    assert active_adapters == {ShadowAdapter.SGS_CSV, ShadowAdapter.SIDRA_V3}
    for candidate_id in (
        "focus:alternate:unverified",
        "b3:alternate:unverified",
        "anbima:txt:unverified",
        "tesouro:alternate:unverified",
    ):
        descriptor = get_candidate_profile(candidate_id)
        assert descriptor.adapter is None
        assert descriptor.status is CandidateStatus.DISABLED
        assert descriptor.disabled_reason


def test_unknown_or_arbitrary_urls_fail_closed() -> None:
    with pytest.raises(ValueError, match="unknown source candidate"):
        get_candidate_profile("https://attacker.invalid/data")


def test_ingestion_eligibility_cannot_be_enabled_by_shadow_artifact_metadata() -> None:
    profile = get_candidate_profile("bcb_sgs:csv:433")
    assert not profile.ingestion_eligible
    assert not profile.enabled_for_ingestion
    assert profile.license_status == "unverified"
