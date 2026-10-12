from datetime import date, datetime, timezone
from pathlib import Path

import pytest

from macro_score.methodology import COMPONENTS_BY_KEY, ComponentUnit, MacroComponentKey
from macro_score.models import (
    ComponentPoint,
    ComponentSeries,
    ComponentUnavailableReason,
)


def _require_normalization() -> None:
    normalization_path = Path(__file__).parents[1] / "macro_score" / "normalization.py"
    if not normalization_path.is_file():
        pytest.fail("[RED XBR-601] Macro Score normalization is missing")


def _component_series(
    key: str,
    historical_values: tuple[float, ...],
    current_value: float,
) -> ComponentSeries:
    history: list[ComponentPoint] = []
    start = date(2024, 9, 1)
    available_at = _available_at()
    unit = _component_unit(key)
    for index, value in enumerate(historical_values):
        absolute_month = start.year * 12 + start.month - 1 + index
        reference = date(absolute_month // 12, absolute_month % 12 + 1, 1)
        history.append(ComponentPoint(
            value=value,
            unit=unit,
            reference_period=reference,
            available_at=available_at,
            input_fingerprint=f"history-{index}",
            inputs=(),
        ))
    current = ComponentPoint(
        value=current_value,
        unit=unit,
        reference_period=date(2026, 8, 31),
        available_at=available_at,
        input_fingerprint="current",
        inputs=(),
    )
    return ComponentSeries(
        key=MacroComponentKey(key),
        current=current,
        history=tuple(history),
        reason=None,
    )


def _available_at() -> datetime:
    return datetime(2026, 9, 1, tzinfo=timezone.utc)


def _component_unit(key: str) -> ComponentUnit:
    return COMPONENTS_BY_KEY[MacroComponentKey(key)].unit


def test_red_xbr_601_type7_winsorization_and_polarity_are_deterministic() -> None:
    _require_normalization()
    from macro_score.normalization import normalize_component, type7_quantile

    values = tuple(float(value) for value in range(24))
    high_component = _component_series("ibc_br_sa_yoy", (*values[:-1], 100.0), 100.0)
    low_component = _component_series("desemprego", (*values[:-1], 100.0), 100.0)

    higher = normalize_component(high_component)
    lower = normalize_component(low_component)

    assert type7_quantile(tuple(float(value) for value in range(0, 101, 10)), 0.02) == 2.0
    assert type7_quantile(tuple(float(value) for value in range(0, 101, 10)), 0.98) == 98.0
    assert higher.percentile == pytest.approx(97.91666666666667)
    assert higher.score == pytest.approx(97.91666666666667)
    assert lower.percentile == pytest.approx(97.91666666666667)
    assert lower.score == pytest.approx(2.08333333333333)


def test_midrank_ties_and_insufficient_distinct_months_are_explicit() -> None:
    _require_normalization()
    from macro_score.normalization import normalize_component

    tied = _component_series("ibc_br_sa_yoy", (5.0,) * 24, 5.0)
    insufficient = _component_series("ibc_br_sa_yoy", (5.0,) * 23, 5.0)

    tied_result = normalize_component(tied)
    insufficient_result = normalize_component(insufficient)

    assert tied_result.percentile == 50.0
    assert tied_result.score == 50.0
    assert insufficient_result.current is not None
    assert insufficient_result.current.value == 5.0
    assert insufficient_result.percentile is None
    assert insufficient_result.score is None
    assert insufficient_result.reason is ComponentUnavailableReason.INSUFFICIENT_HISTORY
