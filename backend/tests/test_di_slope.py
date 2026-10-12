import json
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

import pytest

from derived.di_slope import SPEC, build_di_slope_result
from derived.models import UnavailableReason, result_to_payload
from derived.real_rate_inputs import DIContract

AS_OF = datetime(2026, 10, 1, 12, tzinfo=timezone.utc)
TRADE_DATE = date(2026, 9, 30)
FIXTURE = Path(__file__).parent / "fixtures/derived/inclinacao_di_5y_1y.json"


def contract(
    symbol: str,
    maturity: date,
    rate: float,
    *,
    trade_date: date = TRADE_DATE,
) -> DIContract:
    return DIContract(
        trade_date=trade_date,
        symbol=symbol,
        maturity=maturity,
        rate=rate,
        available_at=datetime(2026, 10, 1, 10, tzinfo=timezone.utc),
        availability_basis="first_seen_at",
    )


def curve() -> tuple[DIContract, DIContract]:
    return (
        contract("DI1V27", TRADE_DATE + timedelta(days=365), 11.123456789),
        contract("DI1V31", TRADE_DATE + timedelta(days=1825), 12.987654321),
    )


def test_di_slope_uses_unrounded_same_date_contracts_and_frozen_fixture() -> None:
    # Given
    contracts = curve()

    # When
    result = build_di_slope_result(contracts, as_of=AS_OF)

    # Then
    fixture = json.loads(FIXTURE.read_text(encoding="utf-8"))
    assert result.value == 12.987654321 - 11.123456789
    assert [dict(item.metadata)["symbol"] for item in result.inputs] == ["DI1V27", "DI1V31"]
    assert result_to_payload(result) == fixture


def test_di_slope_selects_both_horizons_from_latest_complete_curve_date() -> None:
    # Given
    previous = date(2026, 9, 29)
    contracts = curve() + (
        contract(
            "DI1V31-OLD",
            previous + timedelta(days=1825),
            99.0,
            trade_date=previous,
        ),
    )

    # When
    result = build_di_slope_result(contracts, as_of=AS_OF)

    # Then
    assert result.value == 12.987654321 - 11.123456789
    assert {item.reference_period for item in result.inputs} == {TRADE_DATE.isoformat()}


@pytest.mark.parametrize("horizon_days", [365, 1825])
@pytest.mark.parametrize("distance_days", [-90, 90])
def test_di_slope_accepts_inclusive_tolerance_for_each_horizon(
    horizon_days: int,
    distance_days: int,
) -> None:
    # Given
    maturities = {
        365: TRADE_DATE + timedelta(days=365),
        1825: TRADE_DATE + timedelta(days=1825),
    }
    maturities[horizon_days] += timedelta(days=distance_days)
    contracts = (
        contract("DI1V27", maturities[365], 11.0),
        contract("DI1V31", maturities[1825], 12.0),
    )

    # When
    result = build_di_slope_result(contracts, as_of=AS_OF)

    # Then
    assert result.value == 1.0


@pytest.mark.parametrize("horizon_days", [365, 1825])
def test_di_slope_rejects_contract_ninety_one_days_outside_each_horizon(
    horizon_days: int,
) -> None:
    # Given
    maturities = {
        365: TRADE_DATE + timedelta(days=365),
        1825: TRADE_DATE + timedelta(days=1825),
    }
    maturities[horizon_days] += timedelta(days=91)
    contracts = (
        contract("DI1V27", maturities[365], 11.0),
        contract("DI1V31", maturities[1825], 12.0),
    )

    # When
    result = build_di_slope_result(contracts, as_of=AS_OF)

    # Then
    assert result.value is None
    assert result.reason is UnavailableReason.DI_CONTRACT_OUTSIDE_TOLERANCE


def test_di_slope_reuses_shared_selector_tie_breaks_for_both_horizons() -> None:
    # Given
    contracts = (
        contract("DI1Z27", TRADE_DATE + timedelta(days=335), 20.0),
        contract("DI1A27", TRADE_DATE + timedelta(days=335), 10.0),
        contract("DI1A31", TRADE_DATE + timedelta(days=1795), 14.0),
        contract("DI1Z31", TRADE_DATE + timedelta(days=1795), 15.0),
    )

    # When
    result = build_di_slope_result(contracts, as_of=AS_OF)

    # Then
    assert result.value == 14.0 - 10.0
    assert {dict(item.metadata)["symbol"] for item in result.inputs} == {
        "DI1A27",
        "DI1A31",
    }


def test_di_slope_does_not_combine_contracts_from_different_trade_dates() -> None:
    # Given
    previous = TRADE_DATE - timedelta(days=1)
    contracts = (
        contract("DI1V27", TRADE_DATE + timedelta(days=365), 11.0),
        contract(
            "DI1V31-OLD",
            previous + timedelta(days=1825),
            12.0,
            trade_date=previous,
        ),
    )

    # When
    result = build_di_slope_result(contracts, as_of=AS_OF)

    # Then
    assert result.value is None
    assert result.reason is UnavailableReason.DI_CONTRACT_OUTSIDE_TOLERANCE


def test_di_slope_reports_missing_curve_and_reused_contract(monkeypatch: pytest.MonkeyPatch) -> None:
    # Given
    reused_contract = contract("DI1S28", TRADE_DATE + timedelta(days=730), 11.0)
    monkeypatch.setattr(
        "derived.di_slope.select_di_contract_for_horizon",
        lambda _contracts, _reference_date, _horizon_days: reused_contract,
    )

    # When
    missing = build_di_slope_result((), as_of=AS_OF)
    reused = build_di_slope_result((reused_contract,), as_of=AS_OF)

    # Then
    assert missing.reason is UnavailableReason.DI_CURVE_MISSING
    assert reused.reason is UnavailableReason.DI_CONTRACT_REUSED
    assert missing.value is reused.value is None
    assert SPEC.key == "inclinacao_di_5y_1y"
