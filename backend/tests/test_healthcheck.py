from datetime import datetime, timedelta, timezone
from unittest.mock import patch

import pytest

from healthcheck import ENDPOINTS, EndpointSpec, check_endpoint, freshness_hours, run, validate_payload


def test_manifest_covers_every_public_page_dataset() -> None:
    paths = {spec.path for spec in ENDPOINTS}
    assert paths == {
        "/api/status",
        "/api/juros",
        "/api/inflacao",
        "/api/ipca-decomposicao/tudo",
        "/api/atividade",
        "/api/cambio",
        "/api/titulos",
        "/api/focus",
        "/api/complementares",
        "/api/tesouro-direto",
        "/api/curvas-di",
        "/api/eleicoes/estados",
    }
    curves = next(spec for spec in ENDPOINTS if spec.path == "/api/curvas-di")
    assert curves.required_paths == (("dates",), ("curves",))
    elections = next(spec for spec in ENDPOINTS if spec.path == "/api/eleicoes/estados")
    assert elections.required_paths == (
        ("national", "candidates"),
        ("national", "history"),
        ("days",),
        ("ufs",),
    )


def test_payload_validation_rejects_empty_nested_series() -> None:
    with pytest.raises(ValueError, match="empty JSON path: data"):
        validate_payload({"data": {"selic": []}}, (("data",),))


def test_payload_validation_accepts_populated_nested_series() -> None:
    validate_payload({"data": {"selic": [{"data": "01/01/2026", "valor": "1"}]}}, (("data",),))


@pytest.mark.parametrize("empty_path", [("national", "candidates"), ("national", "history")])
def test_election_payload_validation_rejects_empty_national_data(empty_path: tuple[str, ...]) -> None:
    payload = {
        "national": {
            "candidates": [{"name": "Candidate"}],
            "history": {"2026-08-01": {"Candidate": 50.0}},
        },
        "days": ["2026-08-01"],
        "ufs": [{"uf": "SP"}],
    }
    payload["national"][empty_path[-1]] = [] if empty_path[-1] == "candidates" else {}
    elections = next(spec for spec in ENDPOINTS if spec.path == "/api/eleicoes/estados")

    with pytest.raises(ValueError, match=f"empty JSON path: {'.'.join(empty_path)}"):
        validate_payload(payload, elections.required_paths)


def test_freshness_hours_accepts_utc_timestamp() -> None:
    updated = datetime.now(timezone.utc) - timedelta(hours=2)
    age = freshness_hours(updated.isoformat())
    assert 1.9 <= age <= 2.1


def test_status_check_rejects_stale_refresh_timestamp() -> None:
    stale = datetime.now(timezone.utc) - timedelta(hours=31)
    with patch("healthcheck.request_json", return_value=(200, {"last_updated": stale.isoformat()}, 0.1)):
        result = check_endpoint(
            "https://api.example.com",
            EndpointSpec("/api/status", (("last_updated",),)),
            max_latency=5,
            max_age_hours=30,
        )
    assert result["status"] == "error"
    assert "last_updated age" in str(result["error"])


def test_endpoint_probes_continue_when_refresh_fails() -> None:
    healthy = {"path": "/api/status", "status": "ok", "http_status": 200, "latency_seconds": 0.1}
    with (
        patch("healthcheck.check_refresh", side_effect=RuntimeError("provider failed")),
        patch("healthcheck.check_endpoint", return_value=healthy) as endpoint_check,
    ):
        report = run("https://api.example.com", refresh=True, max_latency=5, max_age_hours=30)
    assert report["status"] == "error"
    assert report["refresh"] == {"status": "error", "error": "provider failed"}
    assert endpoint_check.call_count == len(ENDPOINTS)
