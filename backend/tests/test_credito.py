import json
from datetime import datetime, timezone
from pathlib import Path
from typing import TypedDict, cast
from unittest.mock import patch

import config
import pytest
from db import store
from db.store import query_sgs, upsert_sgs
from fastapi.testclient import TestClient
from indicator_catalog import INDICATOR_CATALOG, CatalogGroup, IndicatorKey, Provider, Unit, legacy_codes
from main import app
from refresh import RefreshDependencies, daily_refresh


class CreditRecord(TypedDict):
    data: str
    valor: str


class CreditMetadata(TypedDict):
    code: int
    unit: str
    frequency: str
    reference_period: str
    deadline: str
    freshness: str


class CreditContract(TypedDict):
    data: dict[str, list[CreditRecord]]
    status: str
    unavailable: list[str]
    metadata: dict[str, CreditMetadata]


EXPECTED_CODES = {
    "inadimplencia_pf": 21084,
    "endividamento_familias": 29037,
    "comprometimento_renda": 29034,
    "icc_pf": 25353,
    "taxa_media_credito_pf": 20716,
}
EXPECTED_UNITS = {
    "inadimplencia_pf": Unit.PCT,
    "endividamento_familias": Unit.PCT,
    "comprometimento_renda": Unit.PCT,
    "icc_pf": Unit.PCT_ANNUAL,
    "taxa_media_credito_pf": Unit.PCT_ANNUAL,
}
EXPECTED_FRESHNESS_DAYS = {
    "inadimplencia_pf": 45,
    "endividamento_familias": 75,
    "comprometimento_renda": 75,
    "icc_pf": 45,
    "taxa_media_credito_pf": 45,
}


@pytest.fixture(autouse=True)
def isolated_database(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    if store._conn is not None:
        store._conn.close()
    store._conn = None
    monkeypatch.setattr(store, "DB_PATH", tmp_path / "credito.db")
    monkeypatch.setattr(store, "DATABASE_URL", "")
    store.init_db()

    yield

    if store._conn is not None:
        store._conn.close()
    store._conn = None


def _contract() -> CreditContract:
    path = Path(__file__).parent / "fixtures" / "contracts" / "credito.json"
    return cast(CreditContract, json.loads(path.read_text(encoding="utf-8")))


def _seed(*, omit: str | None = None) -> None:
    for name, records in _contract()["data"].items():
        if name != omit:
            upsert_sgs(EXPECTED_CODES[name], records)


def test_red_xbr_201_catalog_has_exact_credit_series_units_and_slas() -> None:
    credit_group = getattr(CatalogGroup, "CREDIT", None)
    credit_codes = getattr(config, "CREDIT", None)
    assert credit_group is not None, "[RED XBR-201] missing credit catalog group"
    assert credit_codes == EXPECTED_CODES, "[RED XBR-201] credit codes drifted"
    assert legacy_codes(credit_group) == EXPECTED_CODES

    specs = [spec for spec in INDICATOR_CATALOG.values() if spec.group is credit_group]
    assert {str(spec.key) for spec in specs} == set(EXPECTED_CODES)
    for name, code in EXPECTED_CODES.items():
        spec = INDICATOR_CATALOG[IndicatorKey(name)]
        assert spec.provider is Provider.BCB_SGS
        assert spec.provider_code == code
        assert spec.unit is EXPECTED_UNITS[name]
        assert spec.freshness_days == EXPECTED_FRESHNESS_DAYS[name]


def test_red_xbr_201_ok_contract_preserves_reference_periods_and_annual_units() -> None:
    _seed()

    with (
        patch("datafetchers.bcb_sgs.httpx.get") as provider_get,
        patch("routes.credito.datetime") as clock,
    ):
        clock.now.return_value = datetime(2026, 9, 21, tzinfo=timezone.utc)
        response = TestClient(app).get("/api/credito")

    assert response.status_code == 200, "[RED XBR-201] credit GET route is missing"
    provider_get.assert_not_called()
    assert response.json() == _contract()
    assert response.json()["data"]["taxa_media_credito_pf"][0]["valor"] == "14.2"
    assert {
        name: metadata["reference_period"]
        for name, metadata in response.json()["metadata"].items()
    } == {
        "inadimplencia_pf": "2026-08-01",
        "endividamento_familias": "2026-07-01",
        "comprometimento_renda": "2026-08-01",
        "icc_pf": "2026-08-01",
        "taxa_media_credito_pf": "2026-09-01",
    }


def test_red_xbr_201_partial_keeps_four_independent_series() -> None:
    _seed(omit="comprometimento_renda")

    response = TestClient(app).get("/api/credito")

    assert response.status_code == 200, "[RED XBR-201] partial data must remain usable"
    body = response.json()
    assert body["status"] == "partial"
    assert body["unavailable"] == ["comprometimento_renda"]
    assert set(body["data"]) == set(EXPECTED_CODES) - {"comprometimento_renda"}


def test_red_xbr_201_zero_series_returns_typed_503() -> None:
    response = TestClient(app).get("/api/credito")

    assert response.status_code == 503, "[RED XBR-201] empty cache must be unavailable"
    assert response.json() == {"error": "dataset_unavailable", "dataset": "credito"}


def test_red_xbr_201_stale_series_remains_visible_with_own_deadline() -> None:
    upsert_sgs(21084, [{"data": "01/01/2020", "valor": "4.1"}])

    response = TestClient(app).get("/api/credito")

    assert response.status_code == 200
    body = response.json()
    assert body["status"] == "partial"
    assert body["data"]["inadimplencia_pf"] == [{"data": "01/01/2020", "valor": "4.1"}]
    assert body["metadata"]["inadimplencia_pf"] == {
        "code": 21084,
        "unit": "pct",
        "frequency": "monthly",
        "reference_period": "2020-01-01",
        "deadline": "2020-03-16",
        "freshness": "stale",
    }


def test_red_xbr_201_malformed_batch_is_rejected() -> None:
    malformed = {name: [None, {"data": "01/08/2026", "valor": "1.0"}] for name in EXPECTED_CODES}

    with patch("routes.credito.query_sgs_batch", return_value=malformed):
        response = TestClient(app).get("/api/credito")

    assert response.status_code == 503, "[RED XBR-201] malformed cache must be rejected"
    assert response.json() == {"error": "dataset_unavailable", "dataset": "credito"}


def test_red_xbr_201_refresh_requires_bearer_auth_in_production(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("XBRAY_ENV", "production")
    monkeypatch.setenv("XBRAY_REFRESH_TOKEN", "server-secret")

    response = TestClient(app).post(
        "/api/credito/refresh",
        headers={"Authorization": "Bearer wrong-secret"},
    )

    assert response.status_code == 401
    assert response.json()["detail"] == "unauthorized"


def test_red_xbr_201_refresh_preserves_last_good_cache_on_provider_failure() -> None:
    _seed()

    with patch(
        "datafetchers.bcb_sgs.get_provider_response",
        side_effect=RuntimeError("provider unavailable"),
    ) as provider_get:
        response = TestClient(app).post("/api/credito/refresh")

    assert response.status_code == 200, "[RED XBR-201] credit refresh route is missing"
    assert response.json()["status"] == "ok"
    assert provider_get.call_count >= len(EXPECTED_CODES)
    assert query_sgs(20716) == [{"data": "01/09/2026", "valor": "14.2"}]


def test_red_xbr_201_daily_refresh_registers_credit_sgs_task() -> None:
    sgs_calls: list[tuple[dict[str, int], str | None, bool]] = []

    def fetch_sgs_batch(
        codes: dict[str, int],
        *,
        start_date: str | None = None,
        use_cache: bool = True,
    ) -> dict[str, list[dict[str, str]]]:
        sgs_calls.append((codes, start_date, use_cache))
        return {name: [{"data": "01/08/2026", "valor": "1.0"}] for name in codes}

    payload = {"data": [{"value": 1}]}
    dependencies = RefreshDependencies(
        fetch_sgs_batch=fetch_sgs_batch,
        fetch_all_focus=lambda **_kwargs: payload,
        fetch_anbima_ima=lambda **_kwargs: payload,
        fetch_di_curves=lambda **_kwargs: payload,
        fetch_state_polls=lambda **_kwargs: payload,
        refresh_treasury_data=lambda: payload,
    )

    result = daily_refresh(force=True, dependencies=dependencies)

    assert (EXPECTED_CODES, "01/01/2011", False) in sgs_calls
    credit_tasks = [task for task in result["tasks"] if task["name"] == "sgs_credito"]
    assert len(credit_tasks) == 1, "[RED XBR-201] refresh must expose sgs_credito"
    assert credit_tasks[0]["status"] == "ok"
