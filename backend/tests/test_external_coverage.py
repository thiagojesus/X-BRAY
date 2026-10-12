import json
from datetime import date, datetime, timezone
from pathlib import Path

import pytest

from derived.models import DerivedResult, UnavailableReason, result_to_payload
from derived.sgs_inputs import SGSObservation
from indicator_catalog import INDICATOR_CATALOG, IndicatorKey

AS_OF = datetime(2026, 9, 27, 12, tzinfo=timezone.utc)
REFERENCE_PERIOD = date(2026, 8, 1)
FIXTURE = Path(__file__).parent / "fixtures" / "derived" / "cobertura_externa.json"


def _observation(
    key: str,
    value: float,
    *,
    month: date = REFERENCE_PERIOD,
) -> SGSObservation:
    spec = INDICATOR_CATALOG[IndicatorKey(key)]
    available_day = 15 if key == "conta_corrente_12m_pib" else 16
    available_at = datetime(2026, 9, available_day, 10, tzinfo=timezone.utc)
    return SGSObservation(
        indicator_key=spec.key,
        provider_code=spec.provider_code,
        value=value,
        unit=spec.unit,
        frequency=spec.frequency,
        reference_period=month,
        source=spec.source_url,
        available_at=available_at,
        availability_basis="first_seen_at",
        source_published_at=None,
        first_seen_at=available_at,
    )


def _observations(
    idp: float = 2.3,
    account: float = -1.6,
    *,
    idp_month: date = REFERENCE_PERIOD,
    account_month: date = REFERENCE_PERIOD,
) -> tuple[SGSObservation, ...]:
    return (
        _observation("idp_12m_pib", idp, month=idp_month),
        _observation("conta_corrente_12m_pib", account, month=account_month),
    )


def _result(observations: tuple[SGSObservation, ...]) -> DerivedResult:
    from derived.external_coverage import build_external_coverage_result

    return build_external_coverage_result(observations, as_of=AS_OF)


def test_latest_month_inputs_produce_unrounded_positive_ratio_and_fixture() -> None:
    result = _result(_observations())

    assert result.value == 2.3 / 1.6
    assert result.unit == "ratio"
    assert result.reference_date == REFERENCE_PERIOD.isoformat()

    baseline_fixture = json.loads(FIXTURE.read_text(encoding="utf-8"))
    assert result_to_payload(result) == baseline_fixture


def test_ratio_retains_full_precision() -> None:
    result = _result(_observations(idp=2.3000000001, account=-1.6))

    assert result.value == 2.3000000001 / 1.6


def test_coverage_aligns_monthly_inputs_with_different_day_labels() -> None:
    observations = _observations(
        idp_month=date(2026, 8, 1),
        account_month=date(2026, 8, 31),
    )

    result = _result(observations)

    assert result.value == 2.3 / 1.6
    assert result.reference_date == "2026-08-01"
    assert {item.reference_period for item in result.inputs} == {"2026-08-01", "2026-08-31"}


@pytest.mark.parametrize(
    ("observations", "reason"),
    (
        ((), UnavailableReason.REQUIRED_INPUT_MISSING),
        ((_observation("idp_12m_pib", 2.3),), UnavailableReason.REQUIRED_INPUT_MISSING),
        (
            (_observation("conta_corrente_12m_pib", -1.6),),
            UnavailableReason.REQUIRED_INPUT_MISSING,
        ),
        (
            _observations(idp_month=date(2026, 7, 1)),
            UnavailableReason.REFERENCE_MONTH_MISMATCH,
        ),
        (
            _observations(account=0.0),
            UnavailableReason.CURRENT_ACCOUNT_NON_NEGATIVE,
        ),
        (
            _observations(account=0.2),
            UnavailableReason.CURRENT_ACCOUNT_NON_NEGATIVE,
        ),
    ),
)
def test_invalid_input_pair_has_typed_unavailable_reason(
    observations: tuple[SGSObservation, ...],
    reason: UnavailableReason,
) -> None:
    result = _result(observations)

    assert result.value is None
    assert result.reason is reason


def test_latest_reference_month_does_not_fall_back_to_older_pair() -> None:
    observations = (
        *_observations(
            idp=1.0,
            account=-2.0,
            idp_month=date(2026, 7, 1),
            account_month=date(2026, 7, 1),
        ),
        _observation("idp_12m_pib", 2.3, month=REFERENCE_PERIOD),
    )

    result = _result(observations)

    assert result.value is None
    assert result.reason is UnavailableReason.REFERENCE_MONTH_MISMATCH


def test_negative_current_account_yields_negative_coverage_when_idp_is_negative() -> None:
    result = _result(_observations(idp=-2.3, account=-1.6))

    assert result.value == -2.3 / 1.6
    assert result.reason is None
