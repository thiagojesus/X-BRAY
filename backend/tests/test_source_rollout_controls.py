from __future__ import annotations

from datetime import date

from fastapi.testclient import TestClient

from candidate_eligibility import APPROVED_CANDIDATE_APPROVALS
from main import app
from source_candidates import CANDIDATE_REGISTRY
from source_rollout import FallbackMode, candidate_readiness


def test_mode_parser_fails_closed_for_invalid_values(monkeypatch) -> None:
    from source_rollout import configured_mode

    monkeypatch.delenv("SOURCE_FALLBACK_MODE", raising=False)
    assert configured_mode() == (FallbackMode.OFF, False)
    monkeypatch.setenv("SOURCE_FALLBACK_MODE", "garbage")
    assert configured_mode() == (FallbackMode.OFF, True)


def test_readiness_explains_empty_registry_and_unverified_rights() -> None:
    profile = CANDIDATE_REGISTRY["bcb_sgs:csv:433"]
    result = candidate_readiness(
        profile,
        mode=FallbackMode.ENABLED,
        mode_configuration_error=False,
    )

    assert result["ready"] is False
    assert "no_static_candidate_approval" in result["blockers"]
    assert "license_evidence_unverified" in result["blockers"]
    assert "candidate_ingestion_disabled" in result["blockers"]
    assert result["official_evidence_available"] is True
    assert APPROVED_CANDIDATE_APPROVALS == {}


def test_readiness_reports_breaker_cooldown_without_claiming_probe(monkeypatch) -> None:
    monkeypatch.setenv("XBRAY_ENV", "local")
    monkeypatch.setattr("main.init_db", lambda: None)
    monkeypatch.setattr(
        "routes.source_candidates.store.source_access_states",
        lambda: {
            "bcb_sgs:primary:primary": {
                "scope_id": "bcb_sgs:primary:primary",
                "state": "open",
                "retry_not_before": "2026-10-26T15:00:00+00:00",
            }
        },
    )
    monkeypatch.setattr(
        "routes.source_candidates.store.source_candidate_activity",
        lambda _limit: {"extractions": [], "attempts": [], "origins": []},
    )

    with TestClient(app) as client:
        response = client.get("/api/source-candidates/readiness")

    assert response.status_code == 200
    body = response.json()
    candidate = next(item for item in body["candidates"] if item["candidate_id"] == "bcb_sgs:csv:433")
    assert "primary_circuit_or_quota_cooldown" not in candidate["blockers"]
    assert candidate["primary_access"]["state"] == "open"
    assert candidate["primary_access"]["probe_lease_active"] is False
    assert body["production_approvals_count"] == 0


def test_unknown_candidate_is_controlled_not_internal_error(monkeypatch) -> None:
    monkeypatch.setenv("XBRAY_ENV", "local")
    monkeypatch.setattr("main.init_db", lambda: None)
    with TestClient(app) as client:
        response = client.post("/api/source-candidates/canary", json={"candidate_id": "missing"})
    assert response.status_code == 404
    assert response.json()["detail"] == "unknown_candidate"


def test_canary_dry_run_is_readonly_and_reports_blockers(monkeypatch) -> None:
    monkeypatch.setenv("XBRAY_ENV", "local")
    monkeypatch.setenv("SOURCE_FALLBACK_MODE", "off")
    monkeypatch.setattr("main.init_db", lambda: None)
    monkeypatch.setattr("routes.source_candidates.store.source_access_states", dict)
    monkeypatch.setattr(
        "routes.source_candidates.store.source_candidate_activity",
        lambda _limit: {"extractions": [], "attempts": [], "origins": []},
    )
    def forbidden_transport(*_args, **_kwargs):
        raise AssertionError("read-only preflight must never call provider HTTP")

    monkeypatch.setattr("provider_http.get_provider_response_bounded", forbidden_transport)

    with TestClient(app) as client:
        response = client.post(
            "/api/source-candidates/canary",
            json={"candidate_id": "bcb_sgs:csv:433", "dry_run": True},
        )

    assert response.status_code == 200
    body = response.json()
    assert body["status"] == "blocked"
    assert body["network_called"] is False
    assert body["business_writes"] == 0
    assert body["freshness_metadata_writes"] == 0
    assert "fallback_mode_off" in body["readiness"]["blockers"]


def test_mode_and_request_fields_cannot_create_canary_approval(monkeypatch) -> None:
    monkeypatch.setenv("XBRAY_ENV", "local")
    monkeypatch.setenv("SOURCE_FALLBACK_MODE", "canary")
    monkeypatch.setattr("main.init_db", lambda: None)
    monkeypatch.setattr("routes.source_candidates.store.source_access_states", dict)
    monkeypatch.setattr(
        "routes.source_candidates.store.source_candidate_activity",
        lambda _limit: {"extractions": [], "attempts": [], "origins": []},
    )
    with TestClient(app) as client:
        wrong_confirmation = client.post(
            "/api/source-candidates/canary",
            json={
                "candidate_id": "bcb_sgs:csv:433",
                "dry_run": False,
                "confirm_write": True,
                "expected_proof_sha256": "a" * 64,
                "expected_contract_version": "acquisition-contract-v1",
                "operation_id": "test-operation-1",
            },
        )
        arbitrary_route = client.post(
            "/api/source-candidates/canary",
            json={"candidate_id": "bcb_sgs:csv:433", "dry_run": True,
                  "url": "https://attacker.invalid/"},
        )

    assert wrong_confirmation.status_code == 409
    assert wrong_confirmation.json()["detail"] == "candidate_approval_confirmation_mismatch"
    assert arbitrary_route.status_code == 422
    assert APPROVED_CANDIDATE_APPROVALS == {}


def test_qualified_canary_route_reports_executor_commit(monkeypatch) -> None:
    from types import SimpleNamespace

    from routes import source_candidates

    monkeypatch.setenv("XBRAY_ENV", "local")
    monkeypatch.setenv("SOURCE_FALLBACK_MODE", "canary")
    monkeypatch.setattr("main.init_db", lambda: None)
    monkeypatch.setattr(source_candidates.store, "source_access_states", dict)
    monkeypatch.setattr(
        source_candidates.store,
        "source_candidate_activity",
        lambda _limit: {"extractions": [], "attempts": [], "origins": []},
    )
    candidate_id = "bcb_sgs:csv:433"
    proof = "a" * 64
    approval = SimpleNamespace(proof_sha256=proof, parser_semantic_sha256="d" * 64)
    monkeypatch.setattr(
        source_candidates,
        "APPROVED_CANDIDATE_APPROVALS",
        {candidate_id: approval},
    )
    monkeypatch.setattr(
        source_candidates,
        "readiness_snapshot",
        lambda **_kwargs: {
            "candidates": [{"candidate_id": candidate_id, "ready": True}],
        },
    )
    monkeypatch.setattr(source_candidates.store, "source_canary_binding", lambda _id: None)
    monkeypatch.setattr(source_candidates, "execute_canary_commit", lambda *_args: source_candidates.CanaryCommitOutcome(
        "qualified-canary-fail-closed", candidate_id, "2026-10-26", 1, 1, 1, 0,
        1, 1, 2, "2026-10-26T12:00:00+00:00", "committed", True,
    ))

    with TestClient(app) as client:
        response = client.post(
            "/api/source-candidates/canary",
            json={
                "candidate_id": candidate_id,
                "dry_run": False,
                "confirm_write": True,
                "expected_proof_sha256": proof,
                "expected_contract_version": source_candidates.CANDIDATE_REGISTRY[
                    candidate_id
                ].source_contract_version,
                "operation_id": "qualified-canary-fail-closed",
            },
        )

    assert response.status_code == 200
    body = response.json()
    assert body["committed"] is True
    assert body["request_count"] == 2
    assert body["selected_route"] == "candidate"
    assert body["full_refresh_watermarks_updated"] is False


def test_shadow_dry_run_uses_only_catalog_id_and_private_ephemeral_artifact(monkeypatch) -> None:
    monkeypatch.setenv("XBRAY_ENV", "local")
    monkeypatch.setenv("SOURCE_FALLBACK_MODE", "shadow")
    monkeypatch.setattr("main.init_db", lambda: None)
    monkeypatch.setattr("routes.source_candidates.store.source_access_states", dict)
    monkeypatch.setattr(
        "routes.source_candidates.store.source_candidate_activity",
        lambda _limit: {"extractions": [], "attempts": [], "origins": []},
    )
    seen: dict[str, object] = {}

    def fake_shadow(candidate_id: str, as_of: date, artifact_path) -> dict[str, object]:
        seen["candidate_id"] = candidate_id
        seen["as_of"] = as_of
        seen["artifact_path"] = artifact_path
        return {
            "status": "equivalent",
            "candidate_profile_sha256": "a" * 64,
            "source_contract_version": "acquisition-contract-v1",
            "network": {"actual_outbound_requests": 2},
            "evidence": {"evidence_status": "equivalent"},
            "eligibility_blockers": ["license_evidence_unverified"],
        }

    monkeypatch.setattr("shadow_ingestion.run_shadow", fake_shadow)
    with TestClient(app) as client:
        response = client.post(
            "/api/source-candidates/canary",
            json={"candidate_id": "bcb_sgs:csv:433", "dry_run": True},
        )

    assert response.status_code == 200
    body = response.json()
    assert body["status"] == "equivalent"
    assert body["network_called"] == 2
    assert body["business_writes"] == 0
    assert seen["candidate_id"] == "bcb_sgs:csv:433"
    assert not seen["artifact_path"].exists()
    assert APPROVED_CANDIDATE_APPROVALS == {}
