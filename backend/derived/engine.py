from collections.abc import Callable, Sequence
from dataclasses import dataclass
from datetime import datetime
from math import isfinite

from derived.models import DerivedInput, DerivedResult, UnavailableReason, utc_datetime


@dataclass(frozen=True, slots=True)
class DerivationSpec:
    key: str
    unit: str
    methodology_version: str
    formula: str
    calculate: Callable[[tuple[DerivedInput, ...]], float]


def calculate(
    spec: DerivationSpec,
    inputs: Sequence[DerivedInput],
    as_of: datetime,
) -> DerivedResult:
    cutoff = utc_datetime(as_of)
    normalized_inputs = tuple(inputs)
    if any(item.available_at > cutoff for item in normalized_inputs):
        return DerivedResult(
            key=spec.key,
            value=None,
            unit=spec.unit,
            as_of=cutoff,
            methodology_version=spec.methodology_version,
            formula=spec.formula,
            inputs=normalized_inputs,
            reason=UnavailableReason.INPUT_NOT_AVAILABLE,
        )
    value = spec.calculate(normalized_inputs)
    return DerivedResult(
        key=spec.key,
        value=value if isfinite(value) else None,
        unit=spec.unit,
        as_of=cutoff,
        methodology_version=spec.methodology_version,
        formula=spec.formula,
        inputs=normalized_inputs,
        reason=None if isfinite(value) else UnavailableReason.NON_FINITE_RESULT,
    )
