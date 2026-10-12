from collections.abc import Sequence
from dataclasses import replace
from datetime import date, datetime
from typing import Final

from derived.engine import DerivationSpec, calculate
from derived.models import DerivedInput, DerivedResult, UnavailableReason, utc_datetime
from derived.sgs_inputs import SGSObservation
from indicator_catalog import Frequency, IndicatorKey, ProviderCode, Unit

RESULT_KEY: Final = "spread_credito_pf_selic"
METHODOLOGY_VERSION: Final = "1.0.0"
FORMULA: Final = "taxa_media_credito_pf - selic_meta"
_CREDIT_KEY: Final = IndicatorKey("taxa_media_credito_pf")
_SELIC_KEY: Final = IndicatorKey("selic_meta")
_CREDIT_CODE: Final = ProviderCode(20716)
_SELIC_CODE: Final = ProviderCode(432)


def _subtract(inputs: tuple[DerivedInput, ...]) -> float:
    values = {item.key: item.value for item in inputs}
    return values["taxa_media_credito_pf"] - values["selic_meta"]


SPEC: Final = DerivationSpec(
    key=RESULT_KEY,
    unit="pp",
    methodology_version=METHODOLOGY_VERSION,
    formula=FORMULA,
    calculate=_subtract,
)


def _observation_input(observation: SGSObservation) -> DerivedInput:
    return DerivedInput(
        key=str(observation.indicator_key),
        value=observation.value,
        unit=observation.unit.value,
        reference_period=observation.reference_period.isoformat(),
        available_at=observation.available_at,
        source=observation.source,
        metadata=(
            ("availability_basis", observation.availability_basis),
            ("first_seen_at", observation.first_seen_at.isoformat()),
            (
                "source_published_at",
                observation.source_published_at.isoformat()
                if observation.source_published_at is not None
                else None,
            ),
            ("provider_code", int(observation.provider_code)),
        ),
    )


def _unavailable(
    *,
    as_of: datetime,
    reason: UnavailableReason,
    inputs: tuple[DerivedInput, ...] = (),
    reference_date: date | None = None,
) -> DerivedResult:
    return DerivedResult(
        key=SPEC.key,
        value=None,
        unit=SPEC.unit,
        as_of=as_of,
        methodology_version=SPEC.methodology_version,
        formula=SPEC.formula,
        inputs=inputs,
        reason=reason,
        reference_date=reference_date.isoformat() if reference_date is not None else None,
    )


def build_credit_spread_result(
    observations: Sequence[SGSObservation],
    *,
    as_of: datetime,
) -> DerivedResult:
    cutoff = utc_datetime(as_of)
    eligible = tuple(item for item in observations if item.available_at <= cutoff)
    credits = tuple(
        item
        for item in eligible
        if item.indicator_key == _CREDIT_KEY
        and item.provider_code == _CREDIT_CODE
        and item.unit is Unit.PCT_ANNUAL
        and item.frequency is Frequency.MONTHLY
    )
    if not credits:
        return _unavailable(
            as_of=cutoff,
            reason=UnavailableReason.REQUIRED_INPUT_MISSING,
        )

    credit = max(
        credits,
        key=lambda item: (
            item.reference_period,
            item.available_at,
            item.first_seen_at,
        ),
    )
    selic = tuple(
        item
        for item in eligible
        if item.indicator_key == _SELIC_KEY
        and item.provider_code == _SELIC_CODE
        and item.unit is Unit.PCT_ANNUAL
        and item.frequency is Frequency.DAILY
        and item.reference_period.year == credit.reference_period.year
        and item.reference_period.month == credit.reference_period.month
    )
    if not selic:
        month_selic = tuple(
            item
            for item in eligible
            if item.indicator_key == _SELIC_KEY
            and item.provider_code == _SELIC_CODE
            and item.unit is Unit.PCT_ANNUAL
            and item.frequency is Frequency.DAILY
        )
        return _unavailable(
            as_of=cutoff,
            reason=(
                UnavailableReason.REFERENCE_MONTH_MISMATCH
                if month_selic
                else UnavailableReason.REQUIRED_INPUT_MISSING
            ),
            inputs=(_observation_input(credit),),
            reference_date=credit.reference_period,
        )

    selected_selic = max(
        selic,
        key=lambda item: (
            item.reference_period,
            item.available_at,
            item.first_seen_at,
        ),
    )
    result = calculate(
        SPEC,
        (_observation_input(credit), _observation_input(selected_selic)),
        cutoff,
    )
    return replace(result, reference_date=credit.reference_period.isoformat())
