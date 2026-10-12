import json
from dataclasses import replace
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import patch

import pytest

from derived.focus_target_distance import SPEC, build_focus_target_distance_result
from derived.inflation_target_inputs import InflationTargetLedgerError, InflationTargetObservation
from derived.inflation_targets import INFLATION_TARGET_RULES
from derived.models import UnavailableReason, result_to_payload
from derived.real_rate_inputs import FocusExpectation

FIXTURE = Path(__file__).parent / "fixtures/derived/distancia_focus_meta.json"
AS_OF = datetime(2026, 10, 1, tzinfo=timezone.utc)
AVAILABLE_AT = datetime(2026, 9, 30, tzinfo=timezone.utc)


def _target_observation(
    reference_date: date,
    available_at: datetime = AVAILABLE_AT,
) -> InflationTargetObservation:
    rule = next(
        rule
        for rule in INFLATION_TARGET_RULES
        if rule.effective_from <= reference_date
        and (rule.effective_to is None or reference_date < rule.effective_to)
    )
    return InflationTargetObservation(
        rule=rule,
        first_seen_at=available_at,
        payload_hash=f"fixture-{rule.rule_id}-{rule.table_version}",
    )


@pytest.mark.parametrize(
    ("reference_date", "rule_id"),
    [
        (date(2015, 6, 1), "bcb-cmn-target-2015"),
        (date(2016, 6, 1), "bcb-cmn-target-2016"),
        (date(2017, 6, 1), "bcb-cmn-target-2017"),
        (date(2018, 6, 1), "bcb-cmn-target-2018"),
        (date(2019, 6, 1), "bcb-cmn-target-2019"),
        (date(2020, 6, 1), "bcb-cmn-target-2020"),
        (date(2021, 6, 1), "bcb-cmn-target-2021"),
        (date(2022, 6, 1), "bcb-cmn-target-2022"),
        (date(2023, 6, 1), "bcb-cmn-target-2023"),
        (date(2024, 12, 31), "bcb-cmn-target-2024"),
        (date(2025, 1, 1), "bcb-continuous-target-2025"),
    ],
)
def test_reference_date_selects_effective_target_rule(
    reference_date: date,
    rule_id: str,
) -> None:
    # Given
    expectation = FocusExpectation(reference_date, 4.123456789, AVAILABLE_AT, "first_seen_at")

    # When
    result = build_focus_target_distance_result(
        (expectation,),
        target_observations=(_target_observation(reference_date),),
        as_of=AS_OF,
    )

    # Then
    assert result.value == 4.123456789 - next(
        rule.center_pct
        for rule in INFLATION_TARGET_RULES
        if rule.rule_id == rule_id
    )
    assert result.reference_date == reference_date.isoformat()
    assert dict(next(item for item in result.inputs if item.key == "inflation_target").metadata)[
        "rule_id"
    ] == rule_id


def test_cutoff_is_distinct_from_focus_reference_date_and_target_first_seen() -> None:
    # Given
    expectation = FocusExpectation(date(2024, 12, 31), 4.25, AVAILABLE_AT, "first_seen_at")
    target_first_seen = datetime(2025, 2, 1, tzinfo=timezone.utc)

    # When
    result = build_focus_target_distance_result(
        (expectation,),
        target_observations=(_target_observation(expectation.reference_date, target_first_seen),),
        as_of=AS_OF,
    )

    # Then
    assert result.as_of == AS_OF
    assert result.reference_date == "2024-12-31"
    assert result.value == 1.25
    target = next(item for item in result.inputs if item.key == "inflation_target")
    assert target.available_at == target_first_seen
    assert dict(target.metadata)["first_seen_at"] == target_first_seen.isoformat()


def test_missing_focus_expectation_is_typed_unavailable() -> None:
    # Given
    expectations: tuple[FocusExpectation, ...] = ()

    # When
    result = build_focus_target_distance_result(
        expectations, target_observations=(), as_of=AS_OF
    )

    # Then
    assert result.value is None
    assert result.reason is UnavailableReason.FOCUS_12M_MISSING


def test_latest_focus_on_unsupported_target_date_returns_missing_target() -> None:
    # Given
    expectation = FocusExpectation(date(2014, 12, 31), 4.5, AVAILABLE_AT, "first_seen_at")

    # When
    result = build_focus_target_distance_result(
        (expectation,), target_observations=(), as_of=AS_OF
    )

    # Then
    assert result.value is None
    assert result.reason is UnavailableReason.INFLATION_TARGET_MISSING


def test_target_first_seen_after_cutoff_returns_engine_unavailable_without_future_metadata() -> None:
    # Given
    expectation = FocusExpectation(date(2025, 1, 1), 4.25, AVAILABLE_AT, "first_seen_at")

    # When
    result = build_focus_target_distance_result(
        (expectation,),
        target_observations=(
            _target_observation(expectation.reference_date, AS_OF.replace(year=2027)),
        ),
        as_of=AS_OF,
    )

    # Then
    assert result.value is None
    assert result.reason is UnavailableReason.INPUT_NOT_AVAILABLE
    assert {item.key for item in result.inputs} == {"expected_inflation"}


def test_exact_subtraction_keeps_full_precision_and_does_not_use_annual_expectations() -> None:
    # Given
    expectation = FocusExpectation(date(2025, 1, 1), 3.123456789, AVAILABLE_AT, "first_seen_at")
    annual_expectation = FocusExpectation(date(2024, 1, 1), 99.0, AVAILABLE_AT, "first_seen_at")

    # When
    result = build_focus_target_distance_result(
        (expectation, annual_expectation),
        target_observations=(
            _target_observation(expectation.reference_date),
            _target_observation(annual_expectation.reference_date),
        ),
        as_of=AS_OF,
    )

    # Then
    assert result.value == 3.123456789 - 3.0
    assert {item.key for item in result.inputs} == {"expected_inflation", "inflation_target"}
    focus = next(item for item in result.inputs if item.key == "expected_inflation")
    assert focus.value == 3.123456789


def test_focus_expectation_after_cutoff_is_not_selected() -> None:
    # Given
    older = FocusExpectation(date(2024, 1, 1), 5.0, AVAILABLE_AT, "first_seen_at")
    future = FocusExpectation(date(2025, 1, 1), 99.0, AS_OF.replace(year=2027), "first_seen_at")

    # When
    result = build_focus_target_distance_result(
        (older, future),
        target_observations=(
            _target_observation(older.reference_date),
            _target_observation(future.reference_date),
        ),
        as_of=AS_OF,
    )

    # Then
    assert result.reference_date == "2024-01-01"
    assert result.value == 5.0 - 3.0


def test_focus_target_distance_matches_frozen_calculation_fixture() -> None:
    # Given
    reference_date = date(2025, 1, 1)
    expectation = FocusExpectation(reference_date, 3.125, AVAILABLE_AT, "first_seen_at")
    target = _target_observation(reference_date)
    fixture = json.loads(FIXTURE.read_text(encoding="utf-8"))

    # When
    result = build_focus_target_distance_result(
        (expectation,), target_observations=(target,), as_of=AS_OF
    )

    # Then
    assert result.value == 0.125
    assert result_to_payload(result) == fixture
    assert fixture["key"] == SPEC.key
    assert fixture["methodology_version"] == SPEC.methodology_version


def test_target_revision_is_selected_by_as_of_and_old_cutoff_replays() -> None:
    reference_date = date(2025, 6, 1)
    first_seen_a = AVAILABLE_AT
    first_seen_b = first_seen_a + timedelta(days=1)
    observation_a = _target_observation(reference_date, first_seen_a)
    rule_b = replace(observation_a.rule, table_version="1.0.1", center_pct=3.25)
    observation_b = InflationTargetObservation(rule_b, first_seen_b, "revision-b")
    expectation = FocusExpectation(reference_date, 3.125, AVAILABLE_AT, "first_seen_at")
    observations = (observation_a, observation_b)
    before_b = first_seen_b - timedelta(microseconds=1)

    result_before = build_focus_target_distance_result(
        (expectation,), target_observations=observations, as_of=before_b
    )
    result_after = build_focus_target_distance_result(
        (expectation,), target_observations=observations, as_of=AS_OF
    )
    replay_before = build_focus_target_distance_result(
        (expectation,), target_observations=observations, as_of=before_b
    )

    assert result_before.value == 3.125 - observation_a.rule.center_pct
    assert result_after.value == 3.125 - rule_b.center_pct
    assert replay_before == result_before
    assert dict(result_after.inputs[1].metadata)["table_version"] == "1.0.1"


def test_latest_target_revision_that_no_longer_covers_date_is_not_resurrected() -> None:
    reference_date = date(2025, 6, 1)
    observation_a = _target_observation(reference_date)
    rule_b = replace(
        observation_a.rule,
        table_version="1.0.1",
        effective_from=date(2026, 1, 1),
    )
    observation_b = InflationTargetObservation(
        rule_b,
        AVAILABLE_AT + timedelta(days=1),
        "revision-b",
    )
    expectation = FocusExpectation(reference_date, 3.125, AVAILABLE_AT, "first_seen_at")

    result = build_focus_target_distance_result(
        (expectation,), target_observations=(observation_a, observation_b), as_of=AS_OF
    )

    assert result.value is None
    assert result.reason is UnavailableReason.INFLATION_TARGET_MISSING


def test_persisted_target_revision_replays_without_current_static_table_lookup() -> None:
    reference_date = date(2025, 6, 1)
    observation = _target_observation(reference_date)
    expectation = FocusExpectation(reference_date, 3.125, AVAILABLE_AT, "first_seen_at")

    with patch("derived.focus_target_distance.target_for_reference_date", return_value=None):
        result = build_focus_target_distance_result(
            (expectation,), target_observations=(observation,), as_of=AS_OF
        )

    assert result.value == 3.125 - observation.rule.center_pct
    assert result.reason is None


def test_conflicting_same_time_target_revisions_are_rejected() -> None:
    reference_date = date(2025, 6, 1)
    observation_a = _target_observation(reference_date)
    rule_b = replace(observation_a.rule, center_pct=3.25)
    observation_b = InflationTargetObservation(
        rule_b,
        observation_a.first_seen_at,
        "revision-b",
    )
    expectation = FocusExpectation(reference_date, 3.125, AVAILABLE_AT, "first_seen_at")

    with pytest.raises(InflationTargetLedgerError, match="ambiguous_revision"):
        build_focus_target_distance_result(
            (expectation,), target_observations=(observation_a, observation_b), as_of=AS_OF
        )


def test_overlapping_target_rules_are_rejected() -> None:
    reference_date = date(2025, 6, 1)
    observation = _target_observation(reference_date)
    overlap_rule = replace(observation.rule, rule_id="overlapping-rule", center_pct=4.0)
    overlapping = InflationTargetObservation(
        overlap_rule,
        observation.first_seen_at,
        "overlapping-payload",
    )
    expectation = FocusExpectation(reference_date, 3.125, AVAILABLE_AT, "first_seen_at")

    with pytest.raises(InflationTargetLedgerError, match="ambiguous_rule"):
        build_focus_target_distance_result(
            (expectation,), target_observations=(observation, overlapping), as_of=AS_OF
        )


def test_equal_target_centers_keep_distinct_transition_rule_provenance() -> None:
    # Given
    before_transition = FocusExpectation(date(2024, 12, 31), 3.25, AVAILABLE_AT, "first_seen_at")
    after_transition = FocusExpectation(date(2025, 1, 1), 3.25, AVAILABLE_AT, "first_seen_at")

    # When
    before = build_focus_target_distance_result(
        (before_transition,),
        target_observations=(_target_observation(before_transition.reference_date),),
        as_of=AS_OF,
    )
    after = build_focus_target_distance_result(
        (after_transition,),
        target_observations=(_target_observation(after_transition.reference_date),),
        as_of=AS_OF,
    )

    # Then
    before_target = next(item for item in before.inputs if item.key == "inflation_target")
    after_target = next(item for item in after.inputs if item.key == "inflation_target")
    assert before.value == after.value == 0.25
    assert dict(before_target.metadata)["rule_id"] == "bcb-cmn-target-2024"
    assert dict(after_target.metadata)["rule_id"] == "bcb-continuous-target-2025"
