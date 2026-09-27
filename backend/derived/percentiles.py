from dataclasses import dataclass
from datetime import datetime
from enum import StrEnum, unique
from math import isfinite
from typing import Final

from derived.models import utc_datetime

MINIMUM_OBSERVATIONS: Final = 24


@unique
class PercentileBand(StrEnum):
    VERY_LOW = "very_low"
    LOW = "low"
    NEUTRAL = "neutral"
    HIGH = "high"
    VERY_HIGH = "very_high"


@dataclass(frozen=True, slots=True)
class PercentileObservation:
    value: float
    as_of: datetime

    def __post_init__(self) -> None:
        object.__setattr__(self, "as_of", utc_datetime(self.as_of))


@dataclass(frozen=True, slots=True)
class PercentileResult:
    percentile: float | None
    band: PercentileBand | None
    reason: str | None


def _ten_year_cutoff(as_of: datetime) -> datetime:
    try:
        return as_of.replace(year=as_of.year - 10)
    except ValueError:
        return as_of.replace(year=as_of.year - 10, day=28)


def _band(percentile: float) -> PercentileBand:
    if percentile < 20:
        return PercentileBand.VERY_LOW
    if percentile < 40:
        return PercentileBand.LOW
    if percentile < 60:
        return PercentileBand.NEUTRAL
    if percentile < 80:
        return PercentileBand.HIGH
    return PercentileBand.VERY_HIGH


def empirical_percentile(
    observations: tuple[PercentileObservation, ...],
    *,
    current_value: float,
    as_of: datetime,
) -> PercentileResult:
    cutoff_end = utc_datetime(as_of)
    cutoff_start = _ten_year_cutoff(cutoff_end)
    values = tuple(
        observation.value
        for observation in observations
        if cutoff_start <= observation.as_of <= cutoff_end and isfinite(observation.value)
    )
    if not isfinite(current_value):
        return PercentileResult(None, None, "non_finite_value")
    if len(values) < MINIMUM_OBSERVATIONS:
        return PercentileResult(None, None, "insufficient_history")
    count_less = sum(value < current_value for value in values)
    count_equal = sum(value == current_value for value in values)
    percentile = 100 * (count_less + 0.5 * count_equal) / len(values)
    return PercentileResult(percentile, _band(percentile), None)
