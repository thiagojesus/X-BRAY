from calendar import monthrange
from collections.abc import Sequence
from dataclasses import replace
from datetime import date, datetime
from typing import Final

from derived.engine import DerivationSpec, calculate
from derived.models import DerivedInput, DerivedResult, UnavailableReason, utc_datetime
from derived.sgs_inputs import SGSObservation
from indicator_catalog import IndicatorKey

PRIMARY_BALANCE_CHANGE_KEY: Final = "variacao_saldo_primario_12m"
METHODOLOGY_VERSION: Final = "1.0.0"
FORMULA: Final = "saldo_primario_12m_pib(t) - saldo_primario_12m_pib(t-12 meses)"
UNIT: Final = "pp"
_INDICATOR_KEY: Final = IndicatorKey("saldo_primario_12m_pib")


def _difference(inputs: tuple[DerivedInput, ...]) -> float:
    values = {item.key: item.value for item in inputs}
    return values["current_balance"] - values["previous_balance"]


SPEC: Final = DerivationSpec(
    key=PRIMARY_BALANCE_CHANGE_KEY,
    unit=UNIT,
    methodology_version=METHODOLOGY_VERSION,
    formula=FORMULA,
    calculate=_difference,
)


def _input(key: str, observation: SGSObservation) -> DerivedInput:
    return DerivedInput(
        key=key,
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


def _previous_period(current_period: date) -> date:
    year = current_period.year - 1
    month = current_period.month
    day = min(current_period.day, monthrange(year, month)[1])
    return date(year, month, day)


def _unavailable(
    *,
    as_of: datetime,
    reason: UnavailableReason,
    reference_date: date | None = None,
    inputs: tuple[DerivedInput, ...] = (),
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


def build_primary_balance_change_result(
    observations: Sequence[SGSObservation],
    *,
    as_of: datetime,
) -> DerivedResult:
    cutoff = utc_datetime(as_of)
    eligible = tuple(
        item
        for item in observations
        if item.indicator_key == _INDICATOR_KEY and item.available_at <= cutoff
    )
    if not eligible:
        return _unavailable(
            as_of=cutoff,
            reason=UnavailableReason.REQUIRED_INPUT_MISSING,
        )

    latest_by_period: dict[date, SGSObservation] = {}
    for observation in eligible:
        existing = latest_by_period.get(observation.reference_period)
        if existing is None or observation.available_at > existing.available_at:
            latest_by_period[observation.reference_period] = observation
    current_period = max(latest_by_period)
    current = latest_by_period[current_period]
    current_input = _input("current_balance", current)
    previous_period = _previous_period(current_period)
    previous = latest_by_period.get(previous_period)
    if previous is None:
        return _unavailable(
            as_of=cutoff,
            reason=UnavailableReason.PRIMARY_BALANCE_PREVIOUS_MONTH_MISSING,
            reference_date=current_period,
            inputs=(current_input,),
        )

    previous_input = _input("previous_balance", previous)
    result = calculate(SPEC, (current_input, previous_input), cutoff)
    return replace(
        result,
        reference_date=current_period.isoformat(),
    )
