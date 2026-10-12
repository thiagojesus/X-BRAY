from datetime import date, datetime, timedelta

from derived.models import DerivedInput
from macro_score.calculator import MacroScoreResult, aggregate_macro_score
from macro_score.component_points import ComponentCalculation, component_point
from macro_score.inputs import score_cutoff
from macro_score.methodology import COMPONENTS
from macro_score.models import ComponentSeries
from macro_score.normalization import NormalizedComponent, normalize_component


def _shift_month(month: date, amount: int) -> date:
    index = month.year * 12 + month.month - 1 + amount
    return date(index // 12, index % 12 + 1, 1)


def build_macro_score_result(
    as_of: datetime,
    calculated_at: datetime,
    current_shift: float = 0.0,
) -> MacroScoreResult:
    cutoff = score_cutoff(as_of)
    available_at = cutoff.as_of - timedelta(hours=1)
    normalized: list[NormalizedComponent] = []
    for spec in COMPONENTS:
        history = tuple(
            component_point(
                spec,
                ComponentCalculation(
                    value=float(index),
                    reference_period=_shift_month(cutoff.score_month, index - 23),
                    available_at=available_at,
                    inputs=(DerivedInput(
                        key=f"history_{index}",
                        value=float(index),
                        unit=spec.unit.value,
                        reference_period=_shift_month(cutoff.score_month, index - 23).isoformat(),
                        available_at=available_at,
                        source="test fixture",
                    ),),
                ),
            )
            for index in range(24)
        )
        current_value = 23.0 + current_shift
        current = component_point(
            spec,
            ComponentCalculation(
                value=current_value,
                reference_period=cutoff.score_month,
                available_at=available_at,
                inputs=(DerivedInput(
                    key="current",
                    value=current_value,
                    unit=spec.unit.value,
                    reference_period=cutoff.score_month.isoformat(),
                    available_at=available_at,
                    source="test fixture",
                ),),
            ),
        )
        normalized.append(
            normalize_component(
                ComponentSeries(spec.key, current, history, None)
            )
        )
    return aggregate_macro_score(tuple(normalized), cutoff, calculated_at)
