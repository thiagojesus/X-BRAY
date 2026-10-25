import json
from datetime import datetime, timezone
from pathlib import Path
from typing import TypedDict, cast
from unittest.mock import patch

import pytest
from fastapi.testclient import TestClient

import config
from db import store
from db.store import query_sgs, upsert_sgs
from indicator_catalog import (
    INDICATOR_CATALOG,
    CatalogGroup,
    Frequency,
    IndicatorKey,
    Polarity,
    Provider,
    Unit,
    legacy_codes,
)
from main import app


class ExternalRecord(TypedDict):
    data: str
    valor: str


class ExternalMetadata(TypedDict):
    code: int
    title: str
    short_label: str
    unit: str
    frequency: str
    source_url: str
    reference_period: str
    deadline: str
    freshness: str


class ExternalContract(TypedDict):
    source: str
    last_updated: str | None
    status: str
    data: dict[str, list[ExternalRecord]]
    metadata: dict[str, ExternalMetadata]
    unavailable: list[str]


EXPECTED_CODES = {
    "conta_corrente_12m_pib": 23079,
    "idp_12m_pib": 23080,
    "idp_mensal_usd": 22885,
    "balanca_comercial_usd": 22707,
    "investimento_carteira_usd": 22905,
    "reservas_internacionais": 3546,
}
EXPECTED_TITLES = {
    "conta_corrente_12m_pib": "Transações Correntes acumulado em 12 meses em relação ao PIB - mensal",
    "idp_12m_pib": "Investimento Direto no País acumulado em 12 meses em relação ao PIB - mensal",
    "idp_mensal_usd": "Investimentos diretos no país - IDP - mensal - líquido",
    "balanca_comercial_usd": "Balança comercial - Balanço de Pagamentos - mensal - saldo",
    "investimento_carteira_usd": "Investimentos em carteira - mensal - líquido",
    "reservas_internacionais": "Reservas internacionais - Total - mensal",
}
EXPECTED_SHORT_LABELS = {
    "conta_corrente_12m_pib": "Conta corrente 12m / PIB",
    "idp_12m_pib": "IDP 12m / PIB",
    "idp_mensal_usd": "IDP mensal",
    "balanca_comercial_usd": "Balança comercial",
    "investimento_carteira_usd": "Investimento em carteira",
    "reservas_internacionais": "Reservas",
}
EXPECTED_UNITS = {
    "conta_corrente_12m_pib": Unit.PCT_GDP,
    "idp_12m_pib": Unit.PCT_GDP,
    "idp_mensal_usd": Unit.USD_MILLION,
    "balanca_comercial_usd": Unit.USD_MILLION,
    "investimento_carteira_usd": Unit.USD_MILLION,
    "reservas_internacionais": Unit.USD_MILLION,
}


@pytest.fixture(autouse=True)
def isolated_database(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    if store._conn is not None:
        store._conn.close()
    store._conn = None
    monkeypatch.setattr(store, "DB_PATH", tmp_path / "setor-externo.db")
    monkeypatch.setattr(store, "DATABASE_URL", "")
    store.init_db()

    yield

    if store._conn is not None:
        store._conn.close()
    store._conn = None


def _contract() -> ExternalContract:
    path = Path(__file__).parent / "fixtures" / "contracts" / "setor_externo.json"
    return cast(ExternalContract, json.loads(path.read_text(encoding="utf-8")))


def _seed(*, omit: frozenset[str] = frozenset()) -> None:
    for name, records in _contract()["data"].items():
        if name not in omit:
            upsert_sgs(EXPECTED_CODES[name], records)


def test_red_xbr_301_catalog_has_exact_official_external_series() -> None:
    external_group = getattr(CatalogGroup, "EXTERNAL", None)
    external_codes = getattr(config, "SETOR_EXTERNO", None)
    assert external_group is not None, "[RED XBR-301] missing external catalog group"
    assert external_codes == EXPECTED_CODES, "[RED XBR-301] external codes drifted"
    assert legacy_codes(external_group) == EXPECTED_CODES

    for name, code in EXPECTED_CODES.items():
        spec = INDICATOR_CATALOG[IndicatorKey(name)]
        assert spec.provider is Provider.BCB_SGS
        assert spec.provider_code == code
        assert spec.label == EXPECTED_TITLES[name]
        assert spec.short_label == EXPECTED_SHORT_LABELS[name]
        assert spec.unit is EXPECTED_UNITS[name]
        assert spec.frequency is Frequency.MONTHLY
        assert spec.freshness_days == 75
        assert spec.transformation is None

    assert INDICATOR_CATALOG[IndicatorKey("investimento_carteira_usd")].polarity is Polarity.NEUTRAL


def test_red_xbr_301_ok_contract_preserves_titles_units_and_raw_signs() -> None:
    _seed()

    with (
        patch("datafetchers.bcb_sgs.httpx.get") as provider_get,
        patch("routes.setor_externo.datetime") as clock,
    ):
        clock.now.return_value = datetime(2026, 9, 21, tzinfo=timezone.utc)
        response = TestClient(app).get("/api/setor-externo")

    assert response.status_code == 200, "[RED XBR-301] external GET route is missing"
    provider_get.assert_not_called()
    assert response.json() == _contract()
    assert response.json()["data"]["conta_corrente_12m_pib"][0]["valor"] == "-2.49"
    assert response.json()["data"]["investimento_carteira_usd"][0]["valor"] == "-221.1"


def test_red_xbr_301_envelope_includes_source_and_latest_reference() -> None:
    _seed()

    response = TestClient(app).get("/api/setor-externo")

    assert response.status_code == 200
    assert response.json()["source"] == "BCB SGS"
    assert response.json()["last_updated"] == "2026-08-01"


def test_red_xbr_301_four_of_six_series_remain_partial_and_distinct() -> None:
    missing = frozenset({"idp_12m_pib", "investimento_carteira_usd"})
    _seed(omit=missing)

    response = TestClient(app).get("/api/setor-externo")

    assert response.status_code == 200
    body = response.json()
    assert body["status"] == "partial"
    assert body["unavailable"] == ["idp_12m_pib", "investimento_carteira_usd"]
    assert set(body["data"]) == set(EXPECTED_CODES) - missing
    assert body["metadata"]["conta_corrente_12m_pib"]["unit"] == "pct_gdp"
    assert body["metadata"]["idp_mensal_usd"]["unit"] == "usd_million"
    assert body["metadata"]["reservas_internacionais"]["unit"] == "usd_million"


def test_red_xbr_301_zero_series_returns_typed_503() -> None:
    response = TestClient(app).get("/api/setor-externo")

    assert response.status_code == 503
    assert response.json() == {"error": "dataset_unavailable", "dataset": "setor_externo"}


def test_red_xbr_301_stale_reserves_remain_visible_with_monthly_deadline() -> None:
    upsert_sgs(3546, [{"data": "01/01/2020", "valor": "356884"}])

    response = TestClient(app).get("/api/setor-externo")

    assert response.status_code == 200
    body = response.json()
    assert body["status"] == "partial"
    assert body["data"]["reservas_internacionais"] == [{"data": "01/01/2020", "valor": "356884.0"}]
    assert body["metadata"]["reservas_internacionais"]["deadline"] == "2020-04-15"
    assert body["metadata"]["reservas_internacionais"]["freshness"] == "stale"


def test_red_xbr_301_malformed_batch_is_rejected() -> None:
    malformed = {name: [None, {"data": "01/08/2026", "valor": "1.0"}] for name in EXPECTED_CODES}

    with patch("routes.setor_externo.query_sgs_batch", return_value=malformed):
        response = TestClient(app).get("/api/setor-externo")

    assert response.status_code == 503
    assert response.json() == {"error": "dataset_unavailable", "dataset": "setor_externo"}


def test_red_xbr_301_refresh_requires_bearer_auth_in_production(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("XBRAY_ENV", "production")
    monkeypatch.setenv("XBRAY_REFRESH_TOKEN", "server-secret")

    response = TestClient(app).post(
        "/api/setor-externo/refresh",
        headers={"Authorization": "Bearer wrong-secret"},
    )

    assert response.status_code == 401
    assert response.json()["detail"] == "unauthorized"


def test_red_xbr_301_failed_idp_refresh_preserves_reserves_and_returns_partial() -> None:
    _seed(omit=frozenset({"idp_12m_pib"}))
    reserves_before = query_sgs(3546)

    with patch("routes.setor_externo.force_refresh_sgs", return_value=[]):
        response = TestClient(app).post("/api/setor-externo/refresh")

    assert response.status_code == 200
    assert response.json()["status"] == "partial"
    assert response.json()["unavailable"] == ["idp_12m_pib"]
    assert query_sgs(3546) == reserves_before


def test_red_xbr_301_refresh_retains_last_good_cache_and_logs_provider_failure() -> None:
    _seed()

    with patch(
        "datafetchers.bcb_sgs.get_provider_response",
        side_effect=RuntimeError("provider unavailable"),
    ) as provider_get:
        response = TestClient(app).post("/api/setor-externo/refresh")

    assert response.status_code == 200
    assert response.json()["status"] == "ok"
    assert provider_get.call_count >= len(EXPECTED_CODES)
    assert query_sgs(22905) == [{"data": "01/07/2026", "valor": "-221.1"}]


def test_red_xbr_301_new_and_legacy_gets_share_persisted_reserves() -> None:
    _seed()

    with patch("routes.setor_externo.force_refresh_sgs", return_value=query_sgs(3546)):
        refreshed = TestClient(app).post("/api/setor-externo/refresh")
    external = TestClient(app).get("/api/setor-externo")
    legacy = TestClient(app).get("/api/complementares/reservas")

    assert refreshed.status_code == 200
    assert external.status_code == 200
    assert legacy.status_code == 200
    assert external.json()["data"]["reservas_internacionais"][-1] == legacy.json()["data"][-1]
