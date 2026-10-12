from datetime import timedelta
from typing import Final

import pytest

from datafetchers.focus import FOCUS_12M_KEY
from derived.inflation_target_inputs import InflationTargetObservation
from derived.inflation_targets import InflationTargetRule, target_for_reference_date
from indicator_catalog import INDICATOR_CATALOG, IndicatorKey
from macro_score.calculator import MacroScoreResult, aggregate_macro_score
from macro_score.component_inputs import build_target_distance_series
from macro_score.inputs import ScoreVintage, score_cutoff
from macro_score.methodology import COMPONENTS, COMPONENTS_BY_KEY, MacroComponentKey
from macro_score.models import ComponentUnavailableReason
from macro_score.normalization import normalize_component
from macro_score.sources import MacroScoreSources
from tests.test_macro_score_golden import GoldenFixture, fixture_points, load_fixture

_TARGET_COMPONENT_KEYS: Final = (
    MacroComponentKey("abs_ipca_12m_target"),
    MacroComponentKey("abs_focus_12m_target"),
)


def _target_vintages(fixture: GoldenFixture) -> tuple[ScoreVintage, ...]:
    ipca_spec = INDICATOR_CATALOG[IndicatorKey("ipca_12m")]
    references = (*fixture.sample_periods, fixture.current_reference_period)
    vintages: list[ScoreVintage] = []
    for series_key, provider, unit, value, source in (
        ("ipca_12m", ipca_spec.provider.value, ipca_spec.unit.value, 4.0, "BCB SGS fixture"),
        (FOCUS_12M_KEY, "focus", "pct_annual", 3.5, "BCB Focus fixture"),
    ):
        for reference_period in references:
            current = reference_period == fixture.current_reference_period
            vintages.append(ScoreVintage(
                provider=provider,
                series_key=series_key,
                reference_period=reference_period,
                value=value,
                unit=unit,
                source=source,
                available_at=(
                    fixture.current_available_at if current else fixture.sample_available_at
                ),
                first_seen_at=(
                    fixture.current_available_at if current else fixture.sample_available_at
                ),
                payload_hash=f"{series_key}:{reference_period.isoformat()}",
            ))
    return tuple(vintages)


def _target_observations(
    fixture: GoldenFixture,
    future_rule_id: str | None,
) -> tuple[InflationTargetObservation, ...]:
    references = (*fixture.sample_periods, fixture.current_reference_period)
    rules: dict[str, InflationTargetRule] = {}
    for reference_period in references:
        rule = target_for_reference_date(reference_period)
        assert rule is not None
        rules[rule.rule_id] = rule
    return tuple(
        InflationTargetObservation(
            rule=rule,
            first_seen_at=(
                fixture.as_of + timedelta(seconds=1)
                if rule.rule_id == future_rule_id
                else fixture.sample_available_at
            ),
            payload_hash=f"target:{rule.rule_id}",
        )
        for rule in rules.values()
    )


def _score_with_target_observations(
    fixture: GoldenFixture,
    observations: tuple[InflationTargetObservation, ...],
) -> MacroScoreResult:
    cutoff = score_cutoff(fixture.as_of)
    sources = MacroScoreSources(_target_vintages(fixture), observations, ())
    component_series = {
        series.key: series
        for series in fixture_points(fixture)
    }
    for key in _TARGET_COMPONENT_KEYS:
        component_series[key] = build_target_distance_series(
            COMPONENTS_BY_KEY[key],
            sources,
            cutoff,
        )
    normalized = tuple(
        normalize_component(component_series[spec.key])
        for spec in COMPONENTS
    )
    return aggregate_macro_score(normalized, cutoff, fixture.calculated_at)


def test_future_target_availability_reduces_macro_score_coverage_without_lookahead() -> None:
    # Given
    fixture = load_fixture("2026-10.json")
    current_rule = target_for_reference_date(fixture.current_reference_period)
    assert current_rule is not None
    available_targets = _target_observations(fixture, None)
    late_targets = _target_observations(fixture, current_rule.rule_id)

    # When
    available = _score_with_target_observations(fixture, available_targets)
    late = _score_with_target_observations(fixture, late_targets)

    # Then
    assert available.overall_coverage == 1.0
    assert available.score is not None
    assert late.overall_coverage == pytest.approx(5 / 6)
    assert late.valid_dimensions == 5
    assert late.score is not None
    assert late.score != available.score
    assert {
        component.reason
        for component in late.components
        if component.spec.key in _TARGET_COMPONENT_KEYS
    } == {ComponentUnavailableReason.INFLATION_TARGET_NOT_OBSERVED}
