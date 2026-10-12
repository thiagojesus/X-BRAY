import json
from datetime import date, datetime, timezone
from pathlib import Path

import pytest

from derived.credit_spread import SPEC, build_credit_spread_result
from derived.models import UnavailableReason, result_to_payload
from derived.sgs_inputs import SGSObservation
from indicator_catalog import Frequency, IndicatorKey, ProviderCode, Unit

_AS_OF = datetime(2026, 9, 30, 23, 59, tzinfo=timezone.utc)
_FIXTURE = Path(__file__).parent / "fixtures" / "derived" / "spread_credito_pf_selic.json"


def _observation(
    *,
    key: str,
    code: int,
    value: float,
    reference_period: date,
    available_at: datetime,
) -> SGSObservation:
    return SGSObservation(
        indicator_key=IndicatorKey(key),
        provider_code=ProviderCode(code),
        value=value,
        unit=Unit.PCT_ANNUAL,
        frequency=Frequency.MONTHLY if key == "taxa_media_credito_pf" else Frequency.DAILY,
        reference_period=reference_period,
        source=f"https://api.bcb.gov.br/series/{code}",
        available_at=available_at,
        availability_basis="first_seen_at",
        source_published_at=None,
        first_seen_at=available_at,
    )


def _credit(value: float = 25.123456) -> SGSObservation:
    return _observation(
        key="taxa_media_credito_pf",
        code=20716,
        value=value,
        reference_period=date(2026, 8, 1),
        available_at=datetime(2026, 9, 15, 12, tzinfo=timezone.utc),
    )


def _selic(value: float, reference_period: date) -> SGSObservation:
    return _observation(
        key="selic_meta",
        code=432,
        value=value,
        reference_period=reference_period,
        available_at=datetime(2026, 9, 16, 12, tzinfo=timezone.utc),
    )


def test_credit_spread_subtracts_latest_same_month_selic_without_rounding() -> None:
    # Given
    credit = _credit()
    same_month_selic = _selic(15.987654, date(2026, 8, 31))

    # When
    result = build_credit_spread_result((credit, same_month_selic), as_of=_AS_OF)

    # Then
    assert result.value == 25.123456 - 15.987654
    assert result.unit == "pp"
    assert result.reference_date == "2026-08-01"
    assert result.reason is None
    assert {item.key: item.reference_period for item in result.inputs} == {
        "taxa_media_credito_pf": "2026-08-01",
        "selic_meta": "2026-08-31",
    }


def test_credit_spread_uses_last_daily_selic_in_reference_month() -> None:
    # Given
    credit = _credit(20.0)
    early_selic = _selic(13.0, date(2026, 8, 3))
    late_selic = _selic(14.0, date(2026, 8, 31))

    # When
    result = build_credit_spread_result(
        (credit, late_selic, early_selic), as_of=_AS_OF
    )

    # Then
    assert result.value == 6.0
    assert next(item for item in result.inputs if item.key == "selic_meta").reference_period == (
        "2026-08-31"
    )


def test_credit_spread_uses_latest_eligible_vintage_for_each_series() -> None:
    # Given
    earlier_credit = _credit(20.0)
    latest_credit = _observation(
        key="taxa_media_credito_pf",
        code=20716,
        value=21.0,
        reference_period=date(2026, 8, 1),
        available_at=datetime(2026, 9, 20, 12, tzinfo=timezone.utc),
    )
    earlier_selic = _observation(
        key="selic_meta",
        code=432,
        value=13.0,
        reference_period=date(2026, 8, 31),
        available_at=datetime(2026, 9, 16, 12, tzinfo=timezone.utc),
    )
    latest_selic = _observation(
        key="selic_meta",
        code=432,
        value=15.0,
        reference_period=date(2026, 8, 31),
        available_at=datetime(2026, 9, 21, 12, tzinfo=timezone.utc),
    )

    # When
    result = build_credit_spread_result(
        (earlier_credit, latest_credit, earlier_selic, latest_selic), as_of=_AS_OF
    )

    # Then
    assert result.value == 6.0
    assert {item.key: item.value for item in result.inputs} == {
        "taxa_media_credito_pf": 21.0,
        "selic_meta": 15.0,
    }
    assert {item.key: item.available_at for item in result.inputs} == {
        "taxa_media_credito_pf": datetime(2026, 9, 20, 12, tzinfo=timezone.utc),
        "selic_meta": datetime(2026, 9, 21, 12, tzinfo=timezone.utc),
    }


def test_credit_spread_does_not_use_selic_from_another_month() -> None:
    # Given
    observations = (_credit(), _selic(15.0, date(2026, 7, 31)))

    # When
    result = build_credit_spread_result(observations, as_of=_AS_OF)

    # Then
    assert result.value is None
    assert result.reason is UnavailableReason.REFERENCE_MONTH_MISMATCH
    assert {item.key for item in result.inputs} == {"taxa_media_credito_pf"}


@pytest.mark.parametrize(
    ("observations", "reason"),
    [
        ((), UnavailableReason.REQUIRED_INPUT_MISSING),
        ((_credit(),), UnavailableReason.REQUIRED_INPUT_MISSING),
        ((_selic(15.0, date(2026, 8, 31)),), UnavailableReason.REQUIRED_INPUT_MISSING),
        (
            (_credit(), _selic(15.0, date(2026, 7, 31))),
            UnavailableReason.REFERENCE_MONTH_MISMATCH,
        ),
    ],
)
def test_credit_spread_exposes_missing_source_or_month_mismatch(
    observations: tuple[SGSObservation, ...],
    reason: UnavailableReason,
) -> None:
    # Given

    # When
    result = build_credit_spread_result(observations, as_of=_AS_OF)

    # Then
    assert result.value is None
    assert result.reason is reason


def test_credit_spread_result_matches_serialized_fixture() -> None:
    # Given
    observations = (_credit(), _selic(15.0, date(2026, 8, 31)))
    expected = json.loads(_FIXTURE.read_text(encoding="utf-8"))

    # When
    result = build_credit_spread_result(observations, as_of=_AS_OF)

    # Then
    assert result_to_payload(result) == expected
    assert SPEC.key == "spread_credito_pf_selic"
