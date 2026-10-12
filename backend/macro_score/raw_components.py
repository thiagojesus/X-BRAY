from collections.abc import Callable, Mapping
from datetime import date
from types import MappingProxyType
from typing import Final

from indicator_catalog import INDICATOR_CATALOG, IndicatorKey, IndicatorSpec
from macro_score.component_points import (
    calculation_from_inputs,
    component_point,
    score_vintage_input,
)
from macro_score.inputs import (
    AvailableInput,
    InputSelectionReason,
    MacroScoreCutoff,
    ScoreSourcePolicy,
    ScoreVintage,
    UnavailableInput,
    select_current_vintage,
    select_historical_vintages,
    select_known_source_vintages,
    select_latest_source_by_month,
)
from macro_score.methodology import ComponentSpec, MacroComponentKey
from macro_score.models import (
    ComponentPoint,
    ComponentSeries,
    ComponentUnavailableReason,
)
from macro_score.sources import MacroScoreSources

_INPUT_REASON_MAP: Final[Mapping[InputSelectionReason, ComponentUnavailableReason]] = MappingProxyType({
    InputSelectionReason.REQUIRED_INPUT_MISSING: ComponentUnavailableReason.REQUIRED_INPUT_MISSING,
    InputSelectionReason.STALE: ComponentUnavailableReason.STALE_INPUT,
})


def _policy_for_spec(spec: IndicatorSpec) -> ScoreSourcePolicy:
    return ScoreSourcePolicy(
        provider=spec.provider.value,
        series_key=str(spec.key),
        frequency=spec.frequency,
        freshness_days=spec.freshness_days,
    )


def _raw_spec(source_key: str) -> IndicatorSpec:
    return INDICATOR_CATALOG[IndicatorKey(source_key)]


def _month(reference_period: date) -> date:
    return date(reference_period.year, reference_period.month, 1)


def _prior_year_month(reference_period: date) -> date:
    return date(reference_period.year - 1, reference_period.month, 1)


def _source_months(
    vintages: tuple[ScoreVintage, ...],
    cutoff: MacroScoreCutoff,
    source_spec: IndicatorSpec,
) -> Mapping[date, ScoreVintage]:
    known = select_known_source_vintages(
        vintages,
        cutoff,
        policy=_policy_for_spec(source_spec),
    )
    return MappingProxyType({_month(item.reference_period): item for item in select_latest_source_by_month(known)})


def _history_level(
    spec: ComponentSpec,
    vintages: tuple[ScoreVintage, ...],
    cutoff: MacroScoreCutoff,
) -> tuple[ComponentPoint, ...]:
    source_spec = _raw_spec(spec.source_keys[0])
    return tuple(
        component_point(
            spec,
            calculation_from_inputs(
                item.value,
                (score_vintage_input(spec.source_keys[0], item),),
            ),
        )
        for item in select_historical_vintages(vintages, cutoff, source_spec)
    )


def build_level_series(
    spec: ComponentSpec,
    sources: MacroScoreSources,
    cutoff: MacroScoreCutoff,
) -> ComponentSeries:
    source_key = spec.source_keys[0]
    source_spec = _raw_spec(source_key)
    history = _history_level(spec, sources.vintages, cutoff)
    selected = select_current_vintage(sources.vintages, cutoff, source_spec)
    match selected:
        case AvailableInput(observation=observation):
            current = component_point(
                spec,
                calculation_from_inputs(
                    observation.value,
                    (score_vintage_input(source_key, observation),),
                ),
            )
            return ComponentSeries(spec.key, current, history, None)
        case UnavailableInput(reason=reason):
            return ComponentSeries(spec.key, None, history, _INPUT_REASON_MAP[reason])


def _percent_change(current: float, previous: float) -> float | None:
    return None if previous == 0 else ((current / previous) - 1) * 100


def _difference(current: float, previous: float) -> float:
    return current - previous


_CHANGE_FUNCTIONS: Final[Mapping[MacroComponentKey, Callable[[float, float], float | None]]] = MappingProxyType({
    MacroComponentKey("ibc_br_sa_yoy"): _percent_change,
    MacroComponentKey("rendimento_real_yoy"): _percent_change,
    MacroComponentKey("dbgg_yoy_pp"): _difference,
})


def build_change_series(
    spec: ComponentSpec,
    sources: MacroScoreSources,
    cutoff: MacroScoreCutoff,
) -> ComponentSeries:
    source_key = spec.source_keys[0]
    source_spec = _raw_spec(source_key)
    by_month = _source_months(sources.vintages, cutoff, source_spec)
    change = _CHANGE_FUNCTIONS[spec.key]
    window = select_historical_vintages(sources.vintages, cutoff, source_spec)
    samples: list[ComponentPoint] = []
    for current_observation in window:
        previous = by_month.get(_prior_year_month(current_observation.reference_period))
        if previous is None:
            continue
        value = change(current_observation.value, previous.value)
        if value is None:
            continue
        inputs = (
            score_vintage_input("current_value", current_observation),
            score_vintage_input("previous_value", previous),
        )
        samples.append(component_point(spec, calculation_from_inputs(value, inputs)))

    selected = select_current_vintage(sources.vintages, cutoff, source_spec)
    match selected:
        case AvailableInput(observation=current_observation):
            previous = by_month.get(_prior_year_month(current_observation.reference_period))
            if previous is None:
                return ComponentSeries(
                    spec.key,
                    None,
                    tuple(samples),
                    ComponentUnavailableReason.HISTORICAL_LAG_MISSING,
                )
            value = change(current_observation.value, previous.value)
            if value is None:
                return ComponentSeries(
                    spec.key,
                    None,
                    tuple(samples),
                    ComponentUnavailableReason.INVALID_DENOMINATOR,
                )
            inputs = (
                score_vintage_input("current_value", current_observation),
                score_vintage_input("previous_value", previous),
            )
            return ComponentSeries(
                spec.key,
                component_point(spec, calculation_from_inputs(value, inputs)),
                tuple(samples),
                None,
            )
        case UnavailableInput(reason=reason):
            return ComponentSeries(spec.key, None, tuple(samples), _INPUT_REASON_MAP[reason])
