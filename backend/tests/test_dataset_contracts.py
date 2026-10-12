from unittest.mock import patch

from fastapi.testclient import TestClient


def test_red_xbr_003_partial_inflation_keeps_legacy_body_and_reports_quality() -> None:
    # Given
    from main import app

    cached = {
        "ipca": [{"data": "01/08/2026", "valor": "0.2"}],
        "ipca_12m": [],
        "ipca_15": [],
        "inpc": [],
        "igpm": [],
        "igpdi": [],
        "incc_di": [],
    }

    # When
    with (
        patch("routes.inflacao.read_sgs_batch", return_value=cached),
        TestClient(app, raise_server_exceptions=False) as client,
    ):
        legacy = client.get("/api/inflacao")
        quality = client.get("/api/data-quality/inflacao")

    # Then
    assert legacy.status_code == 200
    assert legacy.json() == {
        "source": "BCB SGS",
        "data": {"ipca": cached["ipca"]},
    }
    assert quality.status_code == 200
    assert quality.json()["status"] == "partial"


def test_red_xbr_003_malformed_cached_record_returns_legacy_unavailable() -> None:
    from main import app

    cached = {
        "ipca": [1],
        "ipca_12m": [{"data": "01/08/2026", "valor": "0.2"}],
        "ipca_15": [],
        "inpc": [],
        "igpm": [],
        "igpdi": [],
        "incc_di": [],
    }

    with (
        patch("routes.inflacao.read_sgs_batch", return_value=cached),
        TestClient(app, raise_server_exceptions=False) as client,
    ):
        response = client.get("/api/inflacao")

    assert response.status_code == 503
    assert response.json() == {"error": "dataset_unavailable", "dataset": "inflacao"}


def test_red_xbr_003_optional_only_cache_is_unavailable() -> None:
    from main import app

    cached = {
        "ipca": [],
        "ipca_12m": [],
        "ipca_15": [],
        "inpc": [],
        "igpm": [{"data": "01/08/2026", "valor": "0.2"}],
        "igpdi": [],
        "incc_di": [],
    }

    with (
        patch("routes.inflacao.read_sgs_batch", return_value=cached),
        TestClient(app, raise_server_exceptions=False) as client,
    ):
        response = client.get("/api/inflacao")
        quality = client.get("/api/data-quality/inflacao")

    assert response.status_code == 503
    assert response.json() == {"error": "dataset_unavailable", "dataset": "inflacao"}
    assert quality.status_code == 200
    assert quality.json()["status"] == "unavailable"
