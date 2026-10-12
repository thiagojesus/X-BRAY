from datetime import date, datetime, timedelta, timezone

from datafetchers.focus import FOCUS_12M_KEY
from derived.inflation_target_inputs import InflationTargetObservation
from derived.inflation_targets import target_for_reference_date
from macro_score.component_inputs import build_component_series
from macro_score.inputs import MacroScoreCutoff, ScoreVintage
from macro_score.methodology import MacroComponentKey
from macro_score.models import ComponentUnavailableReason
from macro_score.sources import MacroScoreSources

AS_OF = datetime(2026, 9, 27, 12, tzinfo=timezone.utc)
AVAILABLE_AT = AS_OF - timedelta(hours=1)


def _target_observation(reference_period: date) -> InflationTargetObservation:
    rule = target_for_reference_date(reference_period)
    assert rule is not None
    return InflationTargetObservation(
        rule=rule,
        first_seen_at=AVAILABLE_AT,
        payload_hash=f"target-{rule.rule_id}-{rule.table_version}",
    )


def _focus_vintage(reference_period: date, median: float) -> ScoreVintage:
    return ScoreVintage(
        provider="focus",
        series_key=FOCUS_12M_KEY,
        reference_period=reference_period,
        value=median,
        unit="pct_annual",
        source="Focus IPCA 12m",
        available_at=AVAILABLE_AT,
        first_seen_at=AVAILABLE_AT,
        payload_hash=f"focus-{reference_period.isoformat()}",
    )


def test_focus_distance_uses_effective_target_rule_for_each_reference_period() -> None:
    # Given
    focus = (
        _focus_vintage(date(2024, 12, 25), 4.25),
        _focus_vintage(date(2025, 1, 2), 4.75),
    )
    targets = (
        _target_observation(date(2024, 12, 25)),
        _target_observation(date(2025, 1, 2)),
    )
    sources = MacroScoreSources(focus, targets, ())

    # When
    components = build_component_series(sources, MacroScoreCutoff(AS_OF, date(2026, 9, 1)))
    distance = next(item for item in components if item.key is MacroComponentKey("abs_focus_12m_target"))

    # Then
    assert distance.current is not None
    assert distance.current.value == 1.75
    assert distance.current.reference_period == date(2025, 1, 2)
    assert len(distance.history) == 2
    assert [item.reference_period for item in distance.history] == [
        date(2024, 12, 25),
        date(2025, 1, 2),
    ]
    assert [
        dict(next(input for input in item.inputs if input.key == "inflation_target").metadata)["rule_id"]
        for item in distance.history
    ] == ["bcb-cmn-target-2024", "bcb-continuous-target-2025"]


def test_focus_distance_excludes_target_rule_first_observed_after_cutoff() -> None:
    # Given
    focus = _focus_vintage(date(2025, 1, 2), 4.75)
    rule = target_for_reference_date(focus.reference_period)
    assert rule is not None
    future_target = InflationTargetObservation(
        rule=rule,
        first_seen_at=AS_OF + timedelta(seconds=1),
        payload_hash="future-target",
    )
    sources = MacroScoreSources((focus,), (future_target,), ())

    # When
    components = build_component_series(sources, MacroScoreCutoff(AS_OF, date(2026, 9, 1)))
    distance = next(item for item in components if item.key is MacroComponentKey("abs_focus_12m_target"))

    # Then
    assert distance.current is None
    assert distance.reason is ComponentUnavailableReason.INFLATION_TARGET_NOT_OBSERVED
    assert distance.history == ()
