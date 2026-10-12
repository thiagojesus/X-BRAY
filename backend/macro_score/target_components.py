from collections.abc import Mapping
from datetime import date
from types import MappingProxyType
from typing import Final

from datafetchers.focus import FOCUS_12M_KEY
from derived.inflation_target_inputs import InflationTargetObservation
from derived.inflation_targets import target_for_reference_date
from derived.models import DerivedInput
from indicator_catalog import INDICATOR_CATALOG, Frequency, IndicatorKey
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
    select_current_source,
    select_current_vintage,
    select_historical_source,
    select_historical_vintages,
)
from macro_score.methodology import ComponentSpec
from macro_score.models import (
    ComponentPoint,
    ComponentSeries,
    ComponentUnavailableReason,
)
from macro_score.sources import MacroScoreSources

_FOCUS_INPUT_KEY: Final = "expected_inflation"
_IPCA_INPUT_KEY: Final = "ipca_12m"
_TARGET_INPUT_KEY: Final = "inflation_target"
_FOCUS_POLICY: Final = ScoreSourcePolicy(
    provider="focus",
    series_key=FOCUS_12M_KEY,
    frequency=Frequency.DAILY,
    freshness_days=7,
)
_INPUT_REASON_MAP: Final[Mapping[InputSelectionReason, ComponentUnavailableReason]] = MappingProxyType({
    InputSelectionReason.REQUIRED_INPUT_MISSING: ComponentUnavailableReason.REQUIRED_INPUT_MISSING,
    InputSelectionReason.STALE: ComponentUnavailableReason.STALE_INPUT,
})


def _uses_focus(spec: ComponentSpec) -> bool:
    return spec.source_keys[0] == _FOCUS_INPUT_KEY


def _source_history(
    spec: ComponentSpec,
    sources: MacroScoreSources,
    cutoff: MacroScoreCutoff,
) -> tuple[ScoreVintage, ...]:
    if _uses_focus(spec):
        return select_historical_source(sources.vintages, cutoff, _FOCUS_POLICY)
    indicator = INDICATOR_CATALOG[IndicatorKey(spec.source_keys[0])]
    return select_historical_vintages(sources.vintages, cutoff, indicator)


def _current_expectation(
    spec: ComponentSpec,
    sources: MacroScoreSources,
    cutoff: MacroScoreCutoff,
) -> AvailableInput | UnavailableInput:
    if _uses_focus(spec):
        return select_current_source(sources.vintages, cutoff, _FOCUS_POLICY)
    indicator = INDICATOR_CATALOG[IndicatorKey(spec.source_keys[0])]
    return select_current_vintage(sources.vintages, cutoff, indicator)


def _target_at_reference(
    reference_period: date,
    observations: tuple[InflationTargetObservation, ...],
    cutoff: MacroScoreCutoff,
) -> tuple[InflationTargetObservation | None, ComponentUnavailableReason | None]:
    rule = target_for_reference_date(reference_period)
    if rule is None:
        return None, ComponentUnavailableReason.INFLATION_TARGET_MISSING
    eligible = tuple(
        observation
        for observation in observations
        if observation.rule == rule and observation.available_at <= cutoff.as_of
    )
    if not eligible:
        return None, ComponentUnavailableReason.INFLATION_TARGET_NOT_OBSERVED
    return max(eligible, key=lambda item: (item.first_seen_at, item.payload_hash)), None


def _target_input(
    observation: InflationTargetObservation,
    reference_period: date,
) -> DerivedInput:
    rule = observation.rule
    return DerivedInput(
        key=_TARGET_INPUT_KEY,
        value=rule.center_pct,
        unit="pct_annual",
        reference_period=reference_period.isoformat(),
        available_at=observation.available_at,
        source=rule.direct_source_url or rule.source_url,
        metadata=(
            ("availability_basis", "first_seen_at"),
            ("effective_from", rule.effective_from.isoformat()),
            ("effective_to", rule.effective_to.isoformat() if rule.effective_to else None),
            ("first_seen_at", observation.first_seen_at.isoformat()),
            ("payload_hash", observation.payload_hash),
            ("regime", rule.regime),
            ("rule_id", rule.rule_id),
            ("table_version", rule.table_version),
            ("tolerance_band_pp", rule.tolerance_band_pp),
        ),
    )


def _distance_point(
    spec: ComponentSpec,
    expectation: ScoreVintage,
    target: InflationTargetObservation,
) -> ComponentPoint:
    reference_period = expectation.reference_period
    input_key = _FOCUS_INPUT_KEY if _uses_focus(spec) else _IPCA_INPUT_KEY
    expected_input = score_vintage_input(input_key, expectation)
    target_input = _target_input(target, reference_period)
    inputs = (expected_input, target_input)
    value = abs(expected_input.value - target_input.value)
    return component_point(spec, calculation_from_inputs(value, inputs))


def _distance_history(
    spec: ComponentSpec,
    sources: MacroScoreSources,
    cutoff: MacroScoreCutoff,
) -> tuple[ComponentPoint, ...]:
    points: list[ComponentPoint] = []
    for expectation in _source_history(spec, sources, cutoff):
        target, _reason = _target_at_reference(
            expectation.reference_period,
            sources.target_observations,
            cutoff,
        )
        if target is not None:
            points.append(_distance_point(spec, expectation, target))
    return tuple(points)


def build_target_distance_series(
    spec: ComponentSpec,
    sources: MacroScoreSources,
    cutoff: MacroScoreCutoff,
) -> ComponentSeries:
    history = _distance_history(spec, sources, cutoff)
    selection = _current_expectation(spec, sources, cutoff)
    match selection:
        case AvailableInput(observation=expectation):
            target, reason = _target_at_reference(
                expectation.reference_period,
                sources.target_observations,
                cutoff,
            )
            if target is None:
                return ComponentSeries(spec.key, None, history, reason)
            return ComponentSeries(
                spec.key,
                _distance_point(spec, expectation, target),
                history,
                None,
            )
        case UnavailableInput(reason=reason):
            return ComponentSeries(
                spec.key,
                None,
                history,
                _INPUT_REASON_MAP[reason],
            )
