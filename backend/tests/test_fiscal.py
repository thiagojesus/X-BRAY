import json
from datetime import datetime, timezone
from decimal import Decimal
from functools import partial
from pathlib import Path
from typing import TypedDict, cast
from unittest.mock import MagicMock, patch

import pytest
from fastapi.testclient import TestClient

import config
from datafetchers.bcb_sgs import force_refresh_sgs
from db import store
from db.store import query_sgs, upsert_sgs
from indicator_catalog import (
    INDICATOR_CATALOG,
    CatalogGroup,
    IndicatorKey,
    Provider,
    Transformation,
    legacy_codes,
)
from main import app
from refresh import RefreshDependencies, daily_refresh


class FixtureSeries(TypedDict):
    code: int
    records: list[dict[str, str]]


FiscalFixture = dict[str, FixtureSeries]
EXPECTED_CODES = {
    "dbgg_pib": 13762,
    "dlsp_pib": 4513,
    "saldo_primario_12m_pib": 5793,
    "juros_nominais_12m_pib": 5760,
    "saldo_nominal_12m_pib": 5727,
}
EXPECTED_SIGN_CONVENTION = {
    "dbgg_pib": "debt_positive",
    "dlsp_pib": "debt_positive",
    "saldo_primario_12m_pib": "surplus_positive",
    "juros_nominais_12m_pib": "cost_positive",
    "saldo_nominal_12m_pib": "surplus_positive",
}


@pytest.fixture(autouse=True)
def isolated_database(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    if store._conn is not None:
        store._conn.close()
    store._conn = None
    monkeypatch.setattr(store, "DB_PATH", tmp_path / "fiscal.db")
    monkeypatch.setattr(store, "DATABASE_URL", "")
    store.init_db()

    yield

    if store._conn is not None:
        store._conn.close()
    store._conn = None


def _fixture() -> FiscalFixture:
    path = Path(__file__).parent / "fixtures" / "fiscal_sgs.json"
    return cast(FiscalFixture, json.loads(path.read_text(encoding="utf-8")))


def _seed(*, omit: str | None = None) -> None:
    for name, series in _fixture().items():
        if name != omit:
            upsert_sgs(series["code"], series["records"])


def test_red_xbr_101_catalog_has_exact_fiscal_series_and_transforms() -> None:
    fiscal_group = getattr(CatalogGroup, "FISCAL", None)
    fiscal_codes = getattr(config, "FISCAL", None)
    assert fiscal_group is not None, "[RED XBR-101] missing fiscal catalog group"
    assert fiscal_codes == EXPECTED_CODES, (
        "[RED XBR-101] fiscal codes must match the BCB contract"
    )

    assert legacy_codes(fiscal_group) == EXPECTED_CODES
    specs = [spec for spec in INDICATOR_CATALOG.values() if spec.group is fiscal_group]
    assert {str(spec.key) for spec in specs} == set(EXPECTED_CODES), (
        "[RED XBR-101] fiscal catalog must contain exactly five series"
    )
    for name, code in EXPECTED_CODES.items():
        spec = INDICATOR_CATALOG[IndicatorKey(name)]
        assert spec.provider is Provider.BCB_SGS, (
            "[RED XBR-101] all fiscal series must use SGS"
        )
        assert spec.provider_code == code, "[RED XBR-101] fiscal external code drift"

    invert_sign = getattr(Transformation, "INVERT_SIGN", None)
    assert invert_sign is not None, "[RED XBR-101] missing presentation sign transform"
    assert (
        INDICATOR_CATALOG[IndicatorKey("saldo_primario_12m_pib")].transformation
        is invert_sign
    )
    assert (
        INDICATOR_CATALOG[IndicatorKey("saldo_nominal_12m_pib")].transformation
        is invert_sign
    )
    assert (
        INDICATOR_CATALOG[IndicatorKey("juros_nominais_12m_pib")].transformation is None
    )


def test_red_xbr_101_get_is_cache_only_and_normalizes_signs(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from domain_contracts import evaluate_series_map

    monkeypatch.setattr(
        "routes.fiscal.evaluate_series_map",
        partial(evaluate_series_map, now=datetime(2026, 8, 15, tzinfo=timezone.utc)),
    )
    _seed()

    with patch("datafetchers.bcb_sgs.httpx.get") as provider_get:
        response = TestClient(app).get("/api/fiscal")

    assert response.status_code == 200, "[RED XBR-101] fiscal GET route is missing"
    provider_get.assert_not_called()
    body = response.json()
    values = {name: records[0]["valor"] for name, records in body["data"].items()}
    assert values == {
        "dbgg_pib": "82.56",
        "dlsp_pib": "69.13",
        "saldo_primario_12m_pib": "1.20",
        "juros_nominais_12m_pib": "6.00",
        "saldo_nominal_12m_pib": "-2.50",
    }, "[RED XBR-101] response signs or decimal precision are incorrect"
    assert body["status"] == "ok"
    assert body["unavailable"] == []
    assert {
        name: metadata["sign_convention"] for name, metadata in body["metadata"].items()
    } == EXPECTED_SIGN_CONVENTION, (
        "[RED XBR-101] response must expose auditable sign conventions"
    )
    assert body["metadata"]["dbgg_pib"] == {
        "code": 13762,
        "unit": "pct_gdp",
        "frequency": "monthly",
        "freshness": "ok",
        "sign_convention": "debt_positive",
    }
    assert Decimal(str(query_sgs(5793)[0]["valor"])) == Decimal("-1.20")
    assert Decimal(str(query_sgs(5727)[0]["valor"])) == Decimal("2.50")


def test_red_xbr_101_get_returns_typed_partial_for_four_of_five_series() -> None:
    _seed(omit="saldo_nominal_12m_pib")

    response = TestClient(app).get("/api/fiscal")

    assert response.status_code == 200, (
        "[RED XBR-101] partial fiscal data must remain usable"
    )
    body = response.json()
    assert body["status"] == "partial"
    assert body["unavailable"] == ["saldo_nominal_12m_pib"]
    assert set(body["data"]) == set(EXPECTED_CODES) - {"saldo_nominal_12m_pib"}


def test_red_xbr_101_get_returns_typed_unavailable_for_zero_series() -> None:
    response = TestClient(app).get("/api/fiscal")

    assert response.status_code == 503, (
        "[RED XBR-101] empty fiscal cache must be unavailable"
    )
    assert response.json() == {"error": "dataset_unavailable", "dataset": "fiscal"}


def test_red_xbr_101_get_rejects_malformed_cached_records() -> None:
    malformed = [
        None,
        {"data": "01/07/2026", "valor": "1.00"},
        {"data": "01/07/2026", "valor": "not-a-number", "auxiliary": "1.00"},
    ]

    malformed_series = {name: malformed for name in EXPECTED_CODES}
    with patch("routes.fiscal.query_sgs_batch", return_value=malformed_series):
        response = TestClient(app).get("/api/fiscal")

    assert response.status_code == 503, "[RED XBR-101] malformed cache must be rejected"
    assert response.json() == {"error": "dataset_unavailable", "dataset": "fiscal"}


def test_red_xbr_101_refresh_requires_bearer_auth_in_production(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("XBRAY_ENV", "production")
    monkeypatch.setenv("XBRAY_REFRESH_TOKEN", "server-secret")

    response = TestClient(app).post(
        "/api/fiscal/refresh",
        headers={"Authorization": "Bearer wrong-secret"},
    )

    assert response.status_code == 401, (
        "[RED XBR-101] fiscal refresh must share refresh auth"
    )
    assert response.json()["detail"] == "unauthorized"


def test_red_xbr_101_refresh_keeps_last_good_cache_and_logs_provider_failure() -> None:
    _seed()

    with patch(
        "datafetchers.bcb_sgs.get_provider_response",
        side_effect=RuntimeError("provider unavailable"),
    ) as provider_get:
        response = TestClient(app).post("/api/fiscal/refresh")

    assert response.status_code == 200
    assert response.json()["status"] == "ok"
    assert provider_get.call_count >= len(EXPECTED_CODES)
    assert Decimal(str(query_sgs(5793)[0]["valor"])) == Decimal("-1.20")
    assert Decimal(str(query_sgs(5727)[0]["valor"])) == Decimal("2.50")


def test_red_xbr_101_successful_refresh_persists_provider_signs_raw() -> None:
    response = MagicMock()
    response.json.return_value = [{"data": "01/07/2026", "valor": "-1.20"}]

    with (
        patch(
            "datafetchers.bcb_sgs._make_windows",
            return_value=[("01/07/2026", "31/07/2026")],
        ),
        patch("datafetchers.bcb_sgs.get_provider_response", return_value=response),
    ):
        refreshed = force_refresh_sgs(5793)

    assert Decimal(str(refreshed[0]["valor"])) == Decimal("-1.20")
    assert Decimal(str(query_sgs(5793)[0]["valor"])) == Decimal("-1.20")
    fiscal_response = TestClient(app).get("/api/fiscal")
    assert fiscal_response.status_code == 200
    assert (
        fiscal_response.json()["data"]["saldo_primario_12m_pib"][0]["valor"] == "1.20"
    )


def test_red_xbr_101_daily_refresh_registers_one_fiscal_sgs_task() -> None:
    sgs_calls: list[tuple[dict[str, int], str | None, bool]] = []

    def fetch_sgs_batch(
        codes: dict[str, int],
        *,
        start_date: str | None = None,
        use_cache: bool = True,
    ) -> dict[str, list[dict[str, str]]]:
        sgs_calls.append((codes, start_date, use_cache))
        return {name: [{"data": "01/07/2026", "valor": "1.00"}] for name in codes}

    generic_payload = {"data": [{"value": 1}]}
    dependencies = RefreshDependencies(
        fetch_sgs_batch=fetch_sgs_batch,
        fetch_all_focus=lambda **_kwargs: generic_payload,
        fetch_focus_12m=lambda **_kwargs: generic_payload,
        fetch_anbima_ima=lambda **_kwargs: generic_payload,
        fetch_di_curves=lambda **_kwargs: generic_payload,
        fetch_state_polls=lambda **_kwargs: generic_payload,
        refresh_treasury_data=lambda: generic_payload,
        refresh_sidra_data=lambda: generic_payload,
    )

    result = daily_refresh(force=True, dependencies=dependencies)

    assert (EXPECTED_CODES, "01/01/2001", False) in sgs_calls, (
        "[RED XBR-101] daily refresh must force the complete fiscal SGS history"
    )
    fiscal_tasks = [task for task in result["tasks"] if task["name"] == "sgs_fiscal"]
    assert len(fiscal_tasks) == 1, "[RED XBR-101] refresh status must expose sgs_fiscal"
    assert fiscal_tasks[0]["status"] == "ok"
