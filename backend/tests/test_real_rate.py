from datetime import date, datetime, timedelta, timezone
from unittest.mock import patch

import pytest
from fastapi.testclient import TestClient

from derived.repository import query_snapshot_history, refresh_derived_snapshots
from main import app
from tests.real_rate_support import (
    AS_OF,
    AVAILABLE_AT,
    REAL_RATE_KEY,
    _refresh_result,
    _seed_di,
    _seed_focus,
    isolated_database,
)


def test_red_xbr_502_empty_real_rate_cache_returns_typed_unavailable() -> None:
    # Given
    path = "/api/derivados/juro-real-ex-ante-1y"

    # When
    with (
        patch(
            "datafetchers.focus.get_provider_response",
            side_effect=AssertionError("network access"),
        ),
        patch(
            "datafetchers.b3_di.get_provider_response",
            side_effect=AssertionError("network access"),
        ),
    ):
        response = TestClient(app).get(path)

    # Then
    assert response.status_code == 200, (
        "[RED XBR-502] real-rate derived route is missing"
    )
    assert response.json() == {"status": "unavailable", "data": {}}


def test_persisted_unavailable_real_rate_has_complete_specific_contract() -> None:
    # Given
    assert refresh_derived_snapshots(AS_OF, "cycle-unavailable") == 1

    # When
    response = TestClient(app).get("/api/derivados/juro-real-ex-ante-1y")

    # Then
    assert response.status_code == 200
    assert response.json() == {
        "status": "unavailable",
        "data": {
            "key": REAL_RATE_KEY,
            "value": None,
            "unit": "pct_annual",
            "as_of": AS_OF.isoformat(),
            "methodology_version": "1.0.0",
            "formula": "((1 + DI/100) / (1 + IPCA/100) - 1) * 100",
            "inputs": {},
            "reference_date": None,
            "historical_percentile": None,
            "classification": None,
            "classification_reason": None,
            "unavailable_reason": "focus_12m_missing",
        },
    }


@pytest.mark.parametrize(
    ("nominal_rate", "expected_inflation"),
    [(11.82, 4.66), (11.82, 0.0), (-1.0, 4.66)],
)
def test_real_rate_uses_unrounded_fisher_formula(
    nominal_rate: float,
    expected_inflation: float,
) -> None:
    # Given
    _seed_focus(median=expected_inflation)
    _seed_di(rate=nominal_rate)

    # When
    result = _refresh_result()

    # Then
    expected = ((1 + nominal_rate / 100) / (1 + expected_inflation / 100) - 1) * 100
    assert result.value == expected
    assert result.formula == "((1 + DI/100) / (1 + IPCA/100) - 1) * 100"
    assert result.methodology_version == "1.0.0"


def test_reference_date_and_input_provenance_are_distinct_from_cutoff() -> None:
    # Given
    focus_available = datetime(2026, 9, 27, 9, tzinfo=timezone.utc)
    di_available = datetime(2026, 9, 27, 8, tzinfo=timezone.utc)
    _seed_focus(reference_date="2026-09-25", available_at=focus_available)
    target = date(2026, 9, 25) + timedelta(days=365)
    _seed_di(
        trade_date="2026-09-24",
        maturity=target.isoformat(),
        symbol="DI1V27",
        available_at=di_available,
    )
    _seed_di(
        trade_date="2026-09-26",
        maturity=target.isoformat(),
        symbol="DI1X27",
        available_at=di_available,
    )

    # When
    result = _refresh_result()

    # Then
    assert result.as_of == AS_OF
    assert result.reference_date == "2026-09-25"
    inputs = {item.key: item for item in result.inputs}
    assert inputs["nominal_rate"].reference_period == "2026-09-24"
    assert inputs["nominal_rate"].available_at == di_available
    assert dict(inputs["nominal_rate"].metadata) == {
        "availability_basis": "first_seen_at",
        "maturity": target.isoformat(),
        "symbol": "DI1V27",
    }
    assert inputs["expected_inflation"].reference_period == "2026-09-25"
    assert inputs["expected_inflation"].available_at == focus_available
    assert dict(inputs["expected_inflation"].metadata) == {
        "availability_basis": "first_seen_at",
        "filter": "Indicador eq 'IPCA' and Suavizada eq 'S' and baseCalculo eq 0",
    }


def test_annual_focus_never_falls_back_for_real_rate() -> None:
    # Given
    _seed_focus(series_key="IPCA")
    _seed_di()

    # When
    result = _refresh_result()

    # Then
    assert result.value is None
    assert result.reason is not None
    assert result.reason.value == "focus_12m_missing"


def test_annual_focus_only_route_exposes_focus_12m_missing_reason() -> None:
    _seed_focus(series_key="IPCA")
    _seed_di()
    _refresh_result()

    with (
        patch(
            "datafetchers.focus.get_provider_response",
            side_effect=AssertionError("network access"),
        ),
        patch(
            "datafetchers.b3_di.get_provider_response",
            side_effect=AssertionError("network access"),
        ),
    ):
        response = TestClient(app).get("/api/derivados/juro-real-ex-ante-1y")

    assert response.status_code == 200
    assert response.json()["status"] == "unavailable"
    assert response.json()["data"]["unavailable_reason"] == "focus_12m_missing"


@pytest.mark.parametrize(
    ("late_source", "reason"),
    [("focus", "focus_12m_missing"), ("di", "di_curve_missing")],
)
def test_each_input_must_be_available_at_calculation_cutoff(
    late_source: str,
    reason: str,
) -> None:
    # Given
    future = AS_OF + timedelta(seconds=1)
    _seed_focus(available_at=future if late_source == "focus" else AVAILABLE_AT)
    _seed_di(available_at=future if late_source == "di" else AVAILABLE_AT)

    # When
    result = _refresh_result()

    # Then
    assert result.value is None
    assert result.reason is not None
    assert result.reason.value == reason


@pytest.mark.parametrize("distance_days", [-90, 90])
def test_contract_tolerance_is_inclusive(distance_days: int) -> None:
    # Given
    reference = date(2026, 9, 25)
    target = reference + timedelta(days=365)
    _seed_focus(reference_date=reference.isoformat())
    _seed_di(maturity=(target + timedelta(days=distance_days)).isoformat())

    # When
    result = _refresh_result()

    # Then
    assert result.value is not None
    assert result.reason is None


def test_contract_at_ninety_one_days_is_rejected() -> None:
    # Given
    reference = date(2026, 9, 25)
    target = reference + timedelta(days=365)
    _seed_focus(reference_date=reference.isoformat())
    _seed_di(maturity=(target + timedelta(days=91)).isoformat())

    # When
    result = _refresh_result()

    # Then
    assert result.value is None
    assert result.reason is not None
    assert result.reason.value == "di_contract_outside_tolerance"


def test_contract_tie_prefers_earlier_maturity_then_lexicographic_symbol() -> None:
    # Given
    reference = date(2026, 9, 25)
    target = reference + timedelta(days=365)
    earlier = (target - timedelta(days=30)).isoformat()
    later = (target + timedelta(days=30)).isoformat()
    _seed_focus(reference_date=reference.isoformat())
    _seed_di(maturity=later, symbol="DI1A28", rate=30.0)
    _seed_di(maturity=earlier, symbol="DI1Z27", rate=20.0)
    _seed_di(maturity=earlier, symbol="DI1A27", rate=10.0)

    # When
    result = _refresh_result()

    # Then
    nominal = next(item for item in result.inputs if item.key == "nominal_rate")
    assert nominal.value == 10.0
    assert dict(nominal.metadata)["symbol"] == "DI1A27"


def test_late_di_revision_is_ineligible_for_an_older_cutoff() -> None:
    # Given
    old_cutoff = datetime(2026, 9, 27, 12, tzinfo=timezone.utc)
    new_cutoff = datetime(2026, 9, 28, 12, tzinfo=timezone.utc)
    _seed_focus(available_at=old_cutoff - timedelta(hours=2))
    _seed_di(rate=11.0, available_at=old_cutoff - timedelta(hours=1))
    _seed_di(rate=12.0, available_at=old_cutoff + timedelta(hours=1))

    # When
    old_result = _refresh_result(as_of=old_cutoff, cycle="cycle-old")
    new_result = _refresh_result(as_of=new_cutoff, cycle="cycle-new")

    # Then
    old_nominal = next(item for item in old_result.inputs if item.key == "nominal_rate")
    new_nominal = next(item for item in new_result.inputs if item.key == "nominal_rate")
    assert old_nominal.value == 11.0
    assert new_nominal.value == 12.0
    assert [snapshot.result.value for snapshot in query_snapshot_history(REAL_RATE_KEY)] == [
        old_result.value,
        new_result.value,
    ]


def test_late_focus_revision_is_ineligible_for_an_older_cutoff() -> None:
    # Given
    old_cutoff = datetime(2026, 9, 27, 12, tzinfo=timezone.utc)
    new_cutoff = datetime(2026, 9, 28, 12, tzinfo=timezone.utc)
    _seed_focus(median=4.0, available_at=old_cutoff - timedelta(hours=1))
    _seed_focus(median=5.0, available_at=old_cutoff + timedelta(hours=1))
    _seed_di(available_at=old_cutoff - timedelta(hours=2))

    # When
    old_result = _refresh_result(as_of=old_cutoff, cycle="focus-old")
    new_result = _refresh_result(as_of=new_cutoff, cycle="focus-new")

    # Then
    old_focus = next(item for item in old_result.inputs if item.key == "expected_inflation")
    new_focus = next(item for item in new_result.inputs if item.key == "expected_inflation")
    assert old_focus.value == 4.0
    assert new_focus.value == 5.0


def test_reference_date_uses_earlier_di_maximum_when_focus_is_newer() -> None:
    # Given
    _seed_focus(reference_date="2026-09-25", median=4.0)
    _seed_focus(reference_date="2026-09-26", median=5.0)
    _seed_di(trade_date="2026-09-25")

    # When
    result = _refresh_result()

    # Then
    expected_inflation = next(
        item for item in result.inputs if item.key == "expected_inflation"
    )
    assert result.reference_date == "2026-09-25"
    assert expected_inflation.reference_period == "2026-09-25"
    assert expected_inflation.value == 4.0


pytestmark = pytest.mark.usefixtures(isolated_database.__name__)
