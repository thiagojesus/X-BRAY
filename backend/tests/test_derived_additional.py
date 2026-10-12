from datetime import datetime, timezone
from unittest.mock import patch

import pytest
from fastapi.testclient import TestClient

from datafetchers.focus import FOCUS_12M_KEY
from db import store
from derived import refresh as derived_refresh
from derived.inflation_target_inputs import observe_inflation_target_rules
from derived.repository import query_latest_snapshots
from main import app
from tests.real_rate_support import AS_OF, AVAILABLE_AT, isolated_database

pytestmark = pytest.mark.usefixtures(isolated_database.__name__)


def test_red_xbr_503_coordinator_persists_all_six_indicators_at_one_cutoff() -> None:
    coordinator = getattr(derived_refresh, "refresh_all_derived_snapshots", None)
    assert callable(coordinator), "[RED XBR-503] production refresh lacks the six-indicator coordinator"

    as_of = datetime(2026, 9, 27, 12, tzinfo=timezone.utc)
    calls: list[tuple[str, datetime, str]] = []

    def real_rate(cutoff: datetime, cycle_id: str) -> int:
        calls.append(("juro_real_ex_ante_1y", cutoff, cycle_id))
        return 1

    def additional(key: str, *, as_of: datetime, calculation_cycle_id: str) -> int:
        calls.append((key, as_of, calculation_cycle_id))
        return 1

    with (
        patch.object(derived_refresh, "refresh_derived_snapshots", side_effect=real_rate),
        patch.object(derived_refresh, "refresh_additional_snapshot", side_effect=additional),
    ):
        inserted = coordinator(as_of, "cycle-xbr503")

    assert inserted == 6
    assert [call[0] for call in calls] == [
        "juro_real_ex_ante_1y",
        "cobertura_externa",
        "variacao_saldo_primario_12m",
        "spread_credito_pf_selic",
        "distancia_focus_meta",
        "inclinacao_di_5y_1y",
    ]
    assert all(cutoff == as_of and cycle_id == "cycle-xbr503" for _, cutoff, cycle_id in calls)


def test_coordinator_continues_after_indicator_failure_and_reports_partial_count() -> None:
    coordinator = getattr(derived_refresh, "refresh_all_derived_snapshots", None)
    assert callable(coordinator), "[RED XBR-503] production refresh lacks the six-indicator coordinator"

    calls: list[str] = []

    def real_rate(_cutoff: datetime, _cycle_id: str) -> int:
        calls.append("juro_real_ex_ante_1y")
        return 1

    def additional(key: str, *, as_of: datetime, calculation_cycle_id: str) -> int:
        del as_of, calculation_cycle_id
        calls.append(key)
        if key == "variacao_saldo_primario_12m":
            raise ValueError("sensitive detail")
        return 1

    with (
        patch.object(derived_refresh, "refresh_derived_snapshots", side_effect=real_rate),
        patch.object(derived_refresh, "refresh_additional_snapshot", side_effect=additional),
    ):
        with pytest.raises(derived_refresh.DerivedRefreshError) as captured:
            coordinator(datetime(2026, 9, 27, tzinfo=timezone.utc), "cycle-failure")

    assert captured.value.inserted_count == 5
    assert captured.value.failed_keys == ("variacao_saldo_primario_12m",)
    assert calls[-1] == "inclinacao_di_5y_1y"
    assert "sensitive detail" not in str(captured.value)


def test_aggregate_api_aliases_unavailable_reason_without_removing_reason() -> None:
    with patch("db.store._utc_now", return_value=AVAILABLE_AT.isoformat()):
        store.upsert_sgs(23080, [{"data": "31/08/2026", "valor": "3.5"}])
        store.upsert_sgs(23079, [{"data": "31/08/2026", "valor": "2.5"}])
    derived_refresh.refresh_additional_snapshot(
        "cobertura_externa",
        as_of=AS_OF,
        calculation_cycle_id="coverage-unavailable",
    )

    response = TestClient(app).get("/api/derivados")

    assert response.status_code == 200
    result = response.json()["data"]["cobertura_externa"]
    assert result["value"] is None
    assert result["reason"] == "current_account_non_negative"
    assert result["unavailable_reason"] == result["reason"]


def test_production_coordinator_persists_all_keys_and_aggregate_route_is_cache_only() -> None:
    with patch("db.store._utc_now", return_value=AVAILABLE_AT.isoformat()):
        store.upsert_sgs(23080, [{"data": "31/08/2026", "valor": "3.5"}])
        store.upsert_sgs(23079, [{"data": "31/08/2026", "valor": "-2.5"}])
        store.upsert_sgs(5793, [
            {"data": "31/08/2026", "valor": "-0.15"},
            {"data": "31/08/2025", "valor": "0.2"},
        ])
        store.upsert_sgs(20716, [{"data": "31/08/2026", "valor": "25.12"}])
        store.upsert_sgs(432, [{"data": "31/08/2026", "valor": "15.0"}])
        store.upsert_focus(FOCUS_12M_KEY, [{
            "Indicador": "IPCA",
            "Data": "2026-09-25",
            "Mediana": 4.66,
            "Suavizada": "S",
            "baseCalculo": 0,
        }])
        store.upsert_b3_di([
            {
                "trade_date": "2026-09-25",
                "symbol": "DI1V27",
                "maturity": "2027-09-25",
                "rate": 11.82,
            },
            {
                "trade_date": "2026-09-25",
                "symbol": "DI1V31",
                "maturity": "2031-09-25",
                "rate": 11.42,
            },
        ])
    with patch("derived.inflation_target_inputs._now", return_value=AVAILABLE_AT):
        assert observe_inflation_target_rules() == 11

    assert derived_refresh.refresh_all_derived_snapshots(AS_OF, "cycle-production") == 6
    snapshots = query_latest_snapshots()
    expected_keys = {
        "juro_real_ex_ante_1y",
        "cobertura_externa",
        "variacao_saldo_primario_12m",
        "spread_credito_pf_selic",
        "distancia_focus_meta",
        "inclinacao_di_5y_1y",
    }
    assert set(snapshots) == expected_keys
    assert all(snapshot.result.as_of == AS_OF for snapshot in snapshots.values())
    assert all(snapshot.calculation_cycle_id == "cycle-production" for snapshot in snapshots.values())

    with (
        patch("datafetchers.bcb_sgs.httpx.get", side_effect=AssertionError("network access")),
        patch("datafetchers.focus.get_provider_response", side_effect=AssertionError("network access")),
        patch("datafetchers.b3_di.get_provider_response", side_effect=AssertionError("network access")),
        patch(
            "derived.inflation_target_inputs.observe_inflation_target_rules",
            side_effect=AssertionError("target observation during GET"),
        ),
    ):
        response = TestClient(app).get("/api/derivados")

    assert response.status_code == 200
    payload = response.json()
    assert payload["status"] == "ok"
    assert set(payload["data"]) == expected_keys
    assert payload["data"]["distancia_focus_meta"]["value"] == pytest.approx(1.66)
    for key in expected_keys - {"juro_real_ex_ante_1y"}:
        assert payload["data"][key]["unavailable_reason"] == payload["data"][key]["reason"]
