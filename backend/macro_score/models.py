from dataclasses import dataclass
from datetime import date, datetime
from enum import StrEnum, unique
from math import isfinite

from derived.models import DerivedInput, UnavailableReason, utc_datetime
from macro_score.methodology import ComponentUnit, MacroComponentKey


@unique
class ComponentUnavailableReason(StrEnum):
    REQUIRED_INPUT_MISSING = "required_input_missing"
    STALE_INPUT = "stale_input"
    REFERENCE_MONTH_MISMATCH = "reference_month_mismatch"
    CURRENT_ACCOUNT_NON_NEGATIVE = "current_account_non_negative"
    HISTORICAL_LAG_MISSING = "historical_lag_missing"
    INVALID_DENOMINATOR = "invalid_denominator"
    INFLATION_TARGET_MISSING = "inflation_target_missing"
    INFLATION_TARGET_NOT_OBSERVED = "inflation_target_not_observed"
    DERIVED_SNAPSHOT_MISSING = "derived_snapshot_missing"
    DERIVED_SNAPSHOT_UNAVAILABLE = "derived_snapshot_unavailable"
    INSUFFICIENT_HISTORY = "insufficient_history"


@dataclass(frozen=True, slots=True)
class ComponentPoint:
    value: float
    unit: ComponentUnit
    reference_period: date
    available_at: datetime
    input_fingerprint: str
    inputs: tuple[DerivedInput, ...]

    def __post_init__(self) -> None:
        if not isfinite(self.value) or not self.input_fingerprint:
            raise ValueError("component points require finite values and an input fingerprint")
        object.__setattr__(self, "available_at", utc_datetime(self.available_at))
        object.__setattr__(self, "inputs", tuple(sorted(self.inputs, key=lambda item: item.key)))


@dataclass(frozen=True, slots=True)
class ComponentSeries:
    key: MacroComponentKey
    current: ComponentPoint | None
    history: tuple[ComponentPoint, ...]
    reason: ComponentUnavailableReason | None
    upstream_reason: UnavailableReason | None = None

    def __post_init__(self) -> None:
        if (self.current is None) == (self.reason is None):
            raise ValueError("component series must contain a current value or an unavailable reason")
        if tuple(sorted(self.history, key=lambda item: item.reference_period)) != self.history:
            raise ValueError("component history must be ordered by reference period")
        months = tuple(
            date(item.reference_period.year, item.reference_period.month, 1)
            for item in self.history
        )
        if len(months) != len(set(months)):
            raise ValueError("component history may contain at most one value per month")
