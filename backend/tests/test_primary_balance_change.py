import json
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

from derived.models import UnavailableReason, result_to_payload
from derived.primary_balance_change import SPEC, build_primary_balance_change_result
from derived.sgs_inputs import SGSObservation
from indicator_catalog import Frequency, IndicatorKey, ProviderCode, Unit

AS_OF = datetime(2026, 9, 27, 12, tzinfo=timezone.utc)
CURRENT_PERIOD = date(2026, 8, 31)
PREVIOUS_PERIOD = date(2025, 8, 31)


def _observation(reference_period: date, value: float, available_at: datetime) -> SGSObservation:
    return SGSObservation(
        indicator_key=IndicatorKey("saldo_primario_12m_pib"),
        provider_code=ProviderCode(5793),
        value=value,
        unit=Unit.PCT_GDP,
        frequency=Frequency.MONTHLY,
        reference_period=reference_period,
        source="https://api.bcb.gov.br/dados/serie/bcdata.sgs.5793",
        available_at=available_at,
        availability_basis="first_seen_at",
        source_published_at=None,
        first_seen_at=available_at,
    )


def test_primary_balance_change_uses_normalized_values_and_preserves_precision() -> None:
    # Given
    current = _observation(CURRENT_PERIOD, 1.23456789, AS_OF - timedelta(hours=1))
    previous = _observation(PREVIOUS_PERIOD, 0.12345678, AS_OF - timedelta(days=400))

    # When
    result = build_primary_balance_change_result((current, previous), as_of=AS_OF)

    # Then
    assert result.value == 1.23456789 - 0.12345678
    assert result.unit == "pp"
    assert [item.unit for item in result.inputs] == ["pct_gdp", "pct_gdp"]
    assert result.reference_date == CURRENT_PERIOD.isoformat()
    assert [item.reference_period for item in result.inputs] == [
        CURRENT_PERIOD.isoformat(),
        PREVIOUS_PERIOD.isoformat(),
    ]
    assert [item.available_at for item in result.inputs] == [
        current.available_at,
        previous.available_at,
    ]
    assert result.reason is None


def test_primary_balance_change_returns_required_input_missing_without_current_value() -> None:
    # Given
    observations: tuple[SGSObservation, ...] = ()

    # When
    result = build_primary_balance_change_result(observations, as_of=AS_OF)

    # Then
    assert result.value is None
    assert result.reason is UnavailableReason.REQUIRED_INPUT_MISSING


def test_primary_balance_change_returns_previous_month_missing_with_current_only() -> None:
    # Given
    current = _observation(CURRENT_PERIOD, 0.25, AS_OF - timedelta(hours=1))

    # When
    result = build_primary_balance_change_result((current,), as_of=AS_OF)

    # Then
    assert result.value is None
    assert result.reason is UnavailableReason.PRIMARY_BALANCE_PREVIOUS_MONTH_MISSING
    assert result.reference_date == CURRENT_PERIOD.isoformat()


def test_primary_balance_change_requires_exact_calendar_month_twelve_months_prior() -> None:
    # Given
    tempting_near_month = _observation(date(2025, 7, 31), 0.5, AS_OF - timedelta(days=400))
    current = _observation(CURRENT_PERIOD, 1.5, AS_OF - timedelta(hours=1))

    # When
    result = build_primary_balance_change_result((current, tempting_near_month), as_of=AS_OF)

    # Then
    assert result.value is None
    assert result.reason is UnavailableReason.PRIMARY_BALANCE_PREVIOUS_MONTH_MISSING
    assert result.reference_date == CURRENT_PERIOD.isoformat()


def test_primary_balance_change_excludes_observations_after_cutoff() -> None:
    # Given
    current = _observation(CURRENT_PERIOD, 1.5, AS_OF + timedelta(seconds=1))

    # When
    result = build_primary_balance_change_result((current,), as_of=AS_OF)

    # Then
    assert result.value is None
    assert result.reason is UnavailableReason.REQUIRED_INPUT_MISSING


def test_primary_balance_change_matches_frozen_calculation_fixture() -> None:
    # Given
    fixture_path = Path(__file__).parent / "fixtures/derived/variacao_saldo_primario_12m.json"
    current = _observation(CURRENT_PERIOD, 1.23456789, AS_OF - timedelta(hours=1))
    previous = _observation(PREVIOUS_PERIOD, 0.12345678, AS_OF - timedelta(days=400))

    # When
    fixture = json.loads(fixture_path.read_text(encoding="utf-8"))
    payload = result_to_payload(build_primary_balance_change_result((current, previous), as_of=AS_OF))

    # Then
    assert payload == fixture
    assert fixture["key"] == SPEC.key
    assert fixture["methodology_version"] == SPEC.methodology_version


def test_primary_balance_change_marks_overflowing_difference_unavailable() -> None:
    # Given
    current = _observation(CURRENT_PERIOD, 1e308, AS_OF - timedelta(hours=1))
    previous = _observation(PREVIOUS_PERIOD, -1e308, AS_OF - timedelta(days=400))

    # When
    result = build_primary_balance_change_result((current, previous), as_of=AS_OF)

    # Then
    assert result.value is None
    assert result.reason is UnavailableReason.NON_FINITE_RESULT
