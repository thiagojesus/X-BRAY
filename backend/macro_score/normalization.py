import hashlib
import json
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from math import floor
from types import MappingProxyType
from typing import Final

from macro_score.methodology import COMPONENTS_BY_KEY, ComponentSpec, Direction
from macro_score.models import (
    ComponentPoint,
    ComponentSeries,
    ComponentUnavailableReason,
)

MINIMUM_COMPONENT_HISTORY = 24
LOWER_WINSOR_QUANTILE = 0.02
UPPER_WINSOR_QUANTILE = 0.98
_INVERT_DIRECTION: Final[Mapping[Direction, bool]] = MappingProxyType({
    Direction.HIGHER: False,
    Direction.LOWER: True,
})


@dataclass(frozen=True, slots=True)
class NormalizedComponent:
    spec: ComponentSpec
    current: ComponentPoint | None
    percentile: float | None
    score: float | None
    reason: ComponentUnavailableReason | None
    history_count: int
    history_fingerprint: str


def type7_quantile(values: Sequence[float], probability: float) -> float:
    ordered = sorted(values)
    position = (len(ordered) - 1) * probability
    lower_index = floor(position)
    upper_index = min(lower_index + 1, len(ordered) - 1)
    fraction = position - lower_index
    return ordered[lower_index] + fraction * (ordered[upper_index] - ordered[lower_index])


def _history_fingerprint(history: tuple[ComponentPoint, ...]) -> str:
    payload = [
        {
            "fingerprint": point.input_fingerprint,
            "reference_period": point.reference_period.isoformat(),
            "value": point.value,
        }
        for point in history
    ]
    return hashlib.sha256(
        json.dumps(
            payload,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
            allow_nan=False,
        ).encode("utf-8")
    ).hexdigest()


def _winsorize(value: float, lower: float, upper: float) -> float:
    return min(upper, max(lower, value))


def normalize_component(series: ComponentSeries) -> NormalizedComponent:
    spec = COMPONENTS_BY_KEY[series.key]
    history_count = len(series.history)
    fingerprint = _history_fingerprint(series.history)
    if series.current is None:
        return NormalizedComponent(
            spec,
            None,
            None,
            None,
            series.reason,
            history_count,
            fingerprint,
        )
    if history_count < MINIMUM_COMPONENT_HISTORY:
        return NormalizedComponent(
            spec,
            series.current,
            None,
            None,
            ComponentUnavailableReason.INSUFFICIENT_HISTORY,
            history_count,
            fingerprint,
        )

    historical_values = tuple(point.value for point in series.history)
    lower = type7_quantile(historical_values, LOWER_WINSOR_QUANTILE)
    upper = type7_quantile(historical_values, UPPER_WINSOR_QUANTILE)
    winsorized = tuple(_winsorize(value, lower, upper) for value in historical_values)
    current_value = _winsorize(series.current.value, lower, upper)
    less = sum(value < current_value for value in winsorized)
    equal = sum(value == current_value for value in winsorized)
    percentile = 100 * (less + 0.5 * equal) / len(winsorized)
    score = 100 - percentile if _INVERT_DIRECTION[spec.direction] else percentile
    return NormalizedComponent(
        spec,
        series.current,
        percentile,
        score,
        None,
        history_count,
        fingerprint,
    )
