import json
from pathlib import Path
from typing import cast
from unittest.mock import patch

import pytest
from fastapi.testclient import TestClient

import config
from db import store
from db.store import upsert_sgs
from indicator_catalog import INDICATOR_CATALOG, Frequency, IndicatorKey, Provider, Unit
from main import app

EXPECTED_SECTORAL_CODES = {
    "ibc_br_sa": 24364,
    "ibc_agro_sa": 29602,
    "ibc_industria_sa": 29604,
    "ibc_servicos_sa": 29606,
    "ibc_ex_agro_sa": 29608,
    "rendimento_medio_real": 24382,
}

EXPECTED_TITLES = {
    "ibc_br_sa": "Índice de Atividade Econômica do Banco Central (IBC-Br) - com ajuste sazonal",
    "ibc_agro_sa": "Índice de Atividade Econômica do Banco Central (IBC-Br) Agropecuária - com ajuste sazonal",
    "ibc_industria_sa": "Índice de Atividade Econômica do Banco Central (IBC-Br) Indústria - com ajuste sazonal",
    "ibc_servicos_sa": "Índice de Atividade Econômica do Banco Central (IBC-Br) Serviços - com ajuste sazonal",
    "ibc_ex_agro_sa": "Índice de Atividade Econômica do Banco Central (IBC-Br) Ex-Agropecuária - com ajuste sazonal",
    "rendimento_medio_real": "Rendimento médio real habitual das pessoas ocupadas - PNADC",
}


@pytest.fixture(autouse=True)
def isolated_database(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    if store._conn is not None:
        store._conn.close()
    store._conn = None
    monkeypatch.setattr(store, "DB_PATH", tmp_path / "atividade.db")
    monkeypatch.setattr(store, "DATABASE_URL", "")
    store.init_db()

    yield

    if store._conn is not None:
        store._conn.close()
    store._conn = None


def _fixture() -> dict[str, list[dict[str, str]]]:
    path = Path(__file__).parent / "fixtures" / "activity_sgs.json"
    return cast(dict[str, list[dict[str, str]]], json.loads(path.read_text(encoding="utf-8")))


def _seed_sectoral(*, only: frozenset[str] | None = None) -> None:
    for name, records in _fixture().items():
        if only is None or name in only:
            upsert_sgs(EXPECTED_SECTORAL_CODES[name], records)


def test_catalog_has_exact_official_sectoral_activity_series() -> None:
    assert config.ACTIVITY_SECTORAL == EXPECTED_SECTORAL_CODES

    for name, code in EXPECTED_SECTORAL_CODES.items():
        spec = INDICATOR_CATALOG[IndicatorKey(name)]
        assert spec.provider is Provider.BCB_SGS
        assert spec.provider_code == code
        assert spec.label == EXPECTED_TITLES[name]
        assert spec.unit is (Unit.BRL if name == "rendimento_medio_real" else Unit.INDEX)
        assert spec.frequency is Frequency.MONTHLY
        assert spec.transformation is None


def test_setorial_get_is_cache_only_and_loads_mom_lag_before_filtering() -> None:
    _seed_sectoral()

    with patch("datafetchers.bcb_sgs.httpx.get") as provider_get:
        response = TestClient(app).get(
            "/api/atividade/setorial?mode=mom&start=01/01/2025&end=01/01/2025"
        )

    provider_get.assert_not_called()
    assert response.status_code == 200
    body = response.json()
    assert body["mode"] == "mom"
    assert body["status"] == "ok"
    assert body["data"]["ibc_br_sa"] == [{"data": "01/01/2025", "valor": 10.0}]
    assert all(len(records) == 1 for records in body["data"].values())
    assert body["metadata"]["ibc_br_sa"]["unit"] == "pct"


def test_setorial_yoy_loads_prior_year_and_filters_response_window() -> None:
    _seed_sectoral()

    response = TestClient(app).get(
        "/api/atividade/setorial?mode=yoy&start=01/01/2026&end=01/01/2026"
    )

    assert response.status_code == 200
    assert response.json()["data"]["ibc_br_sa"] == [
        {"data": "01/01/2026", "valor": 10.0}
    ]


def test_setorial_level_preserves_units_and_raw_values() -> None:
    _seed_sectoral()

    response = TestClient(app).get(
        "/api/atividade/setorial?mode=level&start=01/01/2025&end=01/01/2025"
    )

    assert response.status_code == 200
    body = response.json()
    assert body["data"]["ibc_br_sa"] == [{"data": "01/01/2025", "valor": "110.0"}]
    assert body["metadata"]["ibc_br_sa"]["unit"] == "index"
    assert body["metadata"]["rendimento_medio_real"]["unit"] == "brl"


def test_nonempty_required_subset_is_partial() -> None:
    _seed_sectoral(only=frozenset({"ibc_br_sa"}))

    response = TestClient(app).get("/api/atividade/setorial?mode=level")

    assert response.status_code == 200
    body = response.json()
    assert body["status"] == "partial"
    assert set(body["data"]) == {"ibc_br_sa"}
    assert body["unavailable"] == [
        "ibc_agro_sa",
        "ibc_industria_sa",
        "ibc_servicos_sa",
        "ibc_ex_agro_sa",
        "rendimento_medio_real",
    ]


def test_zero_usable_required_series_returns_typed_503() -> None:
    response = TestClient(app).get("/api/atividade/setorial?mode=level")

    assert response.status_code == 503
    assert response.json() == {
        "error": "dataset_unavailable",
        "dataset": "atividade_setorial",
    }


def test_red_xbr_401_empty_cache_sectoral_endpoint_returns_typed_503() -> None:
    with patch("datafetchers.bcb_sgs.httpx.get") as provider_get:
        response = TestClient(app).get("/api/atividade/setorial?mode=level")

    provider_get.assert_not_called()
    assert response.status_code == 503
    assert response.json() == {
        "error": "dataset_unavailable",
        "dataset": "atividade_setorial",
    }


@pytest.mark.parametrize(
    "query",
    [
        "mode=invalid",
        "mode=level&start=2025-01-01",
        "mode=level&start=01/02/2025&end=01/01/2025",
        "mode=level&start=01/01/2015&end=02/01/2025",
    ],
)
def test_invalid_mode_or_window_returns_422(query: str) -> None:
    response = TestClient(app).get(f"/api/atividade/setorial?{query}")

    assert response.status_code == 422


def test_missing_and_zero_lags_are_unavailable_without_nan_or_infinity() -> None:
    upsert_sgs(24364, [{"data": "01/01/2025", "valor": "100"}, {"data": "01/02/2025", "valor": "110"}])
    upsert_sgs(29602, [{"data": "01/01/2025", "valor": "0"}, {"data": "01/02/2025", "valor": "10"}])

    response = TestClient(app).get(
        "/api/atividade/setorial?mode=mom&start=01/02/2025&end=01/02/2025"
    )

    assert response.status_code == 200
    body = response.json()
    assert body["status"] == "partial"
    assert body["data"]["ibc_br_sa"] == [{"data": "01/02/2025", "valor": 10.0}]
    assert "ibc_agro_sa" in body["unavailable"]
    assert "NaN" not in response.text
    assert "Infinity" not in response.text


def test_legacy_activity_payload_remains_exact_with_new_cache_present() -> None:
    legacy_records = {
        "pib": [{"data": "01/01/2025", "valor": "1000"}],
        "ibc_br": [{"data": "01/01/2025", "valor": "102"}],
        "desemprego": [{"data": "01/01/2025", "valor": "6.5"}],
        "resultado_primario": [{"data": "01/01/2025", "valor": "5000"}],
    }
    for name, records in legacy_records.items():
        upsert_sgs(config.ACTIVITY[name], records)
    _seed_sectoral()

    response = TestClient(app).get("/api/atividade")

    assert response.status_code == 200
    assert response.json() == {
        "source": "BCB SGS",
        "data": {
            "pib": [{"data": "01/01/2025", "valor": "1000.0"}],
            "ibc_br": [{"data": "01/01/2025", "valor": "102.0"}],
            "desemprego": [{"data": "01/01/2025", "valor": "6.5"}],
            "resultado_primario": [{"data": "01/01/2025", "valor": "5000.0"}],
        },
    }
