from datetime import timedelta
from unittest.mock import patch

import pytest

from db import store
from derived import refresh as derived_refresh
from derived.inflation_target_inputs import observe_inflation_target_rules
from derived.models import UnavailableReason
from derived.refresh import AdditionalDerivedKey, refresh_additional_snapshot
from derived.repository import query_latest_snapshots, query_snapshot_history
from indicator_catalog import IndicatorKey
from tests.real_rate_support import AS_OF, AVAILABLE_AT, FOCUS_12M_KEY, isolated_database

pytestmark = pytest.mark.usefixtures(isolated_database.__name__)


def _seed_inputs() -> None:
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


def test_additional_refresh_persists_five_independent_snapshots() -> None:
    # Given
    _seed_inputs()
    with patch("derived.inflation_target_inputs._now", return_value=AVAILABLE_AT):
        assert observe_inflation_target_rules() == 11
    expected: tuple[tuple[AdditionalDerivedKey, float], ...] = (
        ("cobertura_externa", 1.4),
        ("variacao_saldo_primario_12m", 0.35),
        ("spread_credito_pf_selic", 10.12),
        ("distancia_focus_meta", 1.66),
        ("inclinacao_di_5y_1y", -0.4),
    )

    # When
    inserted = tuple(
        refresh_additional_snapshot(key, as_of=AS_OF, calculation_cycle_id="cycle-xbr503")
        for key, _value in expected
    )

    # Then
    latest = query_latest_snapshots()
    assert inserted == (1, 1, 1, 1, 1)
    assert len(latest) == 5
    for key, value in expected:
        result = latest[key].result
        assert result.value == pytest.approx(value)
        assert result.as_of == AS_OF
        assert latest[key].calculation_cycle_id == "cycle-xbr503"
        assert latest[key].origin.value == "production_refresh"
        assert result.reason is None


def test_primary_balance_refresh_loads_observations_before_calling_pure_calculator() -> None:
    observations = ()
    with (
        patch("derived.refresh.load_sgs_observations", return_value=observations) as loader,
        patch("derived.refresh.build_primary_balance_change_result") as calculator,
    ):
        derived_refresh._primary_balance_change(AS_OF)

    loader.assert_called_once_with(IndicatorKey("saldo_primario_12m_pib"), as_of=AS_OF)
    calculator.assert_called_once_with(observations, as_of=AS_OF)


def test_unavailable_additional_snapshot_is_persisted_and_idempotent() -> None:
    # Given
    with patch("db.store._utc_now", return_value=AVAILABLE_AT.isoformat()):
        store.upsert_sgs(23080, [{"data": "31/08/2026", "valor": "3.5"}])
        store.upsert_sgs(23079, [{"data": "31/08/2026", "valor": "2.5"}])

    # When
    first = refresh_additional_snapshot(
        "cobertura_externa",
        as_of=AS_OF,
        calculation_cycle_id="unavailable-cycle",
    )
    repeated = refresh_additional_snapshot(
        "cobertura_externa",
        as_of=AS_OF,
        calculation_cycle_id="unavailable-repeat",
    )

    # Then
    history = query_snapshot_history("cobertura_externa")
    assert first == 1
    assert repeated == 0
    assert len(history) == 1
    assert history[0].result.value is None
    assert history[0].result.reason is UnavailableReason.CURRENT_ACCOUNT_NON_NEGATIVE


def test_focus_target_snapshot_is_unavailable_without_a_local_rule_observation() -> None:
    # Given
    with patch("db.store._utc_now", return_value=AVAILABLE_AT.isoformat()):
        store.upsert_focus(FOCUS_12M_KEY, [{
            "Indicador": "IPCA",
            "Data": "2026-09-25",
            "Mediana": 4.66,
            "Suavizada": "S",
            "baseCalculo": 0,
        }])

    # When
    inserted = refresh_additional_snapshot(
        "distancia_focus_meta",
        as_of=AS_OF,
        calculation_cycle_id="target-not-observed",
    )

    # Then
    result = query_latest_snapshots()["distancia_focus_meta"].result
    assert inserted == 1
    assert result.value is None
    assert result.reason is UnavailableReason.INPUT_NOT_AVAILABLE
    assert {item.key for item in result.inputs} == {"expected_inflation"}


def test_external_snapshot_identity_changes_without_mutating_prior_row() -> None:
    # Given
    _seed_inputs()
    with patch("derived.inflation_target_inputs._now", return_value=AVAILABLE_AT):
        observe_inflation_target_rules()
    refresh_additional_snapshot(
        "cobertura_externa",
        as_of=AS_OF,
        calculation_cycle_id="external-first",
    )
    original = query_snapshot_history("cobertura_externa")[0]
    revised_time = AVAILABLE_AT + timedelta(seconds=1)
    with patch("db.store._utc_now", return_value=revised_time.isoformat()):
        store.upsert_sgs(23080, [{"data": "31/08/2026", "valor": "4.0"}])

    # When
    inserted = refresh_additional_snapshot(
        "cobertura_externa",
        as_of=AS_OF + timedelta(seconds=2),
        calculation_cycle_id="external-revised",
    )

    # Then
    history = query_snapshot_history("cobertura_externa")
    assert inserted == 1
    assert len(history) == 2
    assert history[0].result.value == original.result.value == 1.4
    assert history[1].result.value == 1.6
    assert history[0].inputs_hash != history[1].inputs_hash
