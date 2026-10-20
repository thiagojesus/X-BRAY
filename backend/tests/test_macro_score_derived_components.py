from dataclasses import replace
from datetime import date, datetime, timedelta, timezone

import pytest

from derived.models import (
    DerivedInput,
    DerivedResult,
    FutureDerivedInputError,
    SnapshotOrigin,
    UnavailableReason,
)
from derived.repository import DerivedSnapshot
from macro_score.component_inputs import build_component_series
from macro_score.inputs import MacroScoreCutoff
from macro_score.methodology import MacroComponentKey
from macro_score.sources import MacroScoreSources

AS_OF = datetime(2026, 9, 27, 12, tzinfo=timezone.utc)


def _real_rate_snapshot() -> DerivedSnapshot:
    calculated_at = AS_OF - timedelta(hours=1)
    result = DerivedResult(
        key="juro_real_ex_ante_1y",
        value=4.0,
        unit="pct_annual",
        as_of=calculated_at,
        methodology_version="1.0.0",
        formula="((1 + DI/100) / (1 + IPCA/100) - 1) * 100",
        inputs=(DerivedInput(
            key="expected_inflation",
            value=4.66,
            unit="pct_annual",
            reference_period="2026-09-25",
            available_at=calculated_at - timedelta(hours=1),
            source="Focus IPCA 12m",
        ),),
        reason=None,
        reference_date="2026-09-25",
    )
    return DerivedSnapshot(
        result=result,
        inputs_hash="production-hash",
        calculation_cycle_id="cycle-1",
        origin=SnapshotOrigin.PRODUCTION_REFRESH,
        created_at=AS_OF - timedelta(minutes=30),
    )


def test_derived_component_uses_production_snapshot_and_excludes_fixture_or_future_capture() -> None:
    # Given
    production = _real_rate_snapshot()
    test_fixture = replace(
        production,
        result=replace(production.result, value=99.0),
        inputs_hash="fixture-hash",
        origin=SnapshotOrigin.TEST_FIXTURE,
        created_at=AS_OF - timedelta(minutes=5),
    )
    future_capture = replace(
        production,
        result=replace(production.result, value=88.0),
        inputs_hash="future-hash",
        created_at=AS_OF + timedelta(seconds=1),
    )
    sources = MacroScoreSources((), (), (production, test_fixture, future_capture))

    # When
    components = build_component_series(sources, MacroScoreCutoff(AS_OF, date(2026, 9, 1)))
    real_rate = next(item for item in components if item.key is MacroComponentKey("juro_real_ex_ante_1y"))

    # Then
    assert real_rate.current is not None
    assert real_rate.current.value == 4.0
    assert real_rate.current.available_at == production.result.inputs[0].available_at
    assert real_rate.current.reference_period == date(2026, 9, 25)
    assert len(real_rate.history) == 1
    assert real_rate.history[0].input_fingerprint == real_rate.current.input_fingerprint


def test_same_cycle_snapshot_persisted_after_cutoff_is_consumed_without_rewriting_availability() -> None:
    # The refresh's cutoff is shared with derived calculations, but persistence
    # necessarily completes after that cutoff. Input timestamps remain authoritative.
    snapshot = _real_rate_snapshot()
    persisted_after_cutoff = replace(
        snapshot,
        result=replace(snapshot.result, as_of=AS_OF),
        created_at=AS_OF + timedelta(seconds=2),
        calculation_cycle_id="cycle-current",
    )
    sources = MacroScoreSources((), (), (persisted_after_cutoff,))

    components = build_component_series(
        sources,
        MacroScoreCutoff(AS_OF, date(2026, 9, 1)),
        calculation_cycle_id="cycle-current",
    )
    real_rate = next(item for item in components if item.key is MacroComponentKey("juro_real_ex_ante_1y"))

    assert real_rate.current is not None
    assert real_rate.current.value == 4.0
    assert real_rate.current.available_at == snapshot.result.inputs[0].available_at


def test_later_cycle_cannot_consume_snapshot_persisted_after_cutoff() -> None:
    snapshot = _real_rate_snapshot()
    persisted_after_cutoff = replace(
        snapshot,
        created_at=AS_OF + timedelta(seconds=2),
        calculation_cycle_id="cycle-other",
    )
    components = build_component_series(
        MacroScoreSources((), (), (persisted_after_cutoff,)),
        MacroScoreCutoff(AS_OF, date(2026, 9, 1)),
        calculation_cycle_id="cycle-current",
    )
    real_rate = next(item for item in components if item.key is MacroComponentKey("juro_real_ex_ante_1y"))

    assert real_rate.current is None
    assert not real_rate.history


def test_post_cutoff_snapshot_requires_an_explicit_nonblank_matching_cycle() -> None:
    snapshot = _real_rate_snapshot()
    post_cutoff = replace(snapshot, created_at=AS_OF + timedelta(seconds=2))

    for snapshot_cycle, score_cycle in (
        (None, None),
        ("", ""),
        ("   ", "   "),
        ("cycle-other", "cycle-current"),
        ("cycle-current", "cycle-current"),  # Matching identity but a different shared cutoff.
    ):
        candidate = replace(post_cutoff, calculation_cycle_id=snapshot_cycle)
        components = build_component_series(
            MacroScoreSources((), (), (candidate,)),
            MacroScoreCutoff(AS_OF, date(2026, 9, 1)),
            calculation_cycle_id=score_cycle,
        )
        real_rate = next(item for item in components if item.key is MacroComponentKey("juro_real_ex_ante_1y"))
        assert real_rate.current is None
        assert not real_rate.history

    matching_result = replace(post_cutoff.result, as_of=AS_OF)
    matching_cycle = replace(
        post_cutoff,
        result=matching_result,
        calculation_cycle_id="cycle-current",
        created_at=AS_OF + timedelta(seconds=2),
    )
    accepted = build_component_series(
        MacroScoreSources((), (), (matching_cycle,)),
        MacroScoreCutoff(AS_OF, date(2026, 9, 1)),
        calculation_cycle_id="cycle-current",
    )
    accepted_real_rate = next(item for item in accepted if item.key is MacroComponentKey("juro_real_ex_ante_1y"))
    assert accepted_real_rate.current is not None


def test_derived_snapshot_rejects_future_input_even_for_matching_cycle() -> None:
    snapshot = _real_rate_snapshot()
    with pytest.raises(FutureDerivedInputError):
        replace(
            snapshot.result,
            inputs=(replace(snapshot.result.inputs[0], available_at=AS_OF + timedelta(seconds=1)),),
        )


def test_month_history_selects_latest_fresh_reference_when_newer_reference_is_stale() -> None:
    base = _real_rate_snapshot()
    spec_key = "spread_credito_pf_selic"
    fresh_result = replace(
        base.result,
        key=spec_key,
        as_of=datetime(2026, 9, 30, 12, tzinfo=timezone.utc),
        reference_date="2026-07-01",
        unit="pp",
        formula="taxa_media_credito_pf - selic_meta",
    )
    fresh = replace(
        base,
        result=fresh_result,
        inputs_hash="fresh-reference",
        created_at=fresh_result.as_of + timedelta(minutes=1),
    )
    stale_result = replace(
        fresh_result,
        as_of=datetime(2026, 10, 1, 12, tzinfo=timezone.utc),
        reference_date="2026-07-31",
    )
    stale = replace(
        base,
        result=stale_result,
        inputs_hash="stale-reference",
        created_at=stale_result.as_of + timedelta(minutes=1),
    )

    components = build_component_series(
        MacroScoreSources((), (), (fresh, stale)),
        MacroScoreCutoff(datetime(2026, 10, 31, 12, tzinfo=timezone.utc), date(2026, 10, 1)),
    )
    spread = next(item for item in components if item.key is MacroComponentKey(spec_key))

    assert spread.current is None
    assert spread.history
    assert spread.history[0].reference_period == date(2026, 7, 1)


def test_same_reference_unavailable_revision_does_not_resurrect_prior_value() -> None:
    base = _real_rate_snapshot()
    prior = replace(base, inputs_hash="prior-value")
    invalidating_result = replace(
        base.result,
        as_of=AS_OF - timedelta(minutes=10),
        value=None,
        reason=UnavailableReason.FOCUS_12M_MISSING,
    )
    revision = replace(
        base,
        result=invalidating_result,
        inputs_hash="revision-unavailable",
        created_at=invalidating_result.as_of + timedelta(minutes=1),
    )

    components = build_component_series(
        MacroScoreSources((), (), (prior, revision)),
        MacroScoreCutoff(AS_OF, date(2026, 9, 1)),
    )
    real_rate = next(item for item in components if item.key is MacroComponentKey("juro_real_ex_ante_1y"))

    assert real_rate.current is None
    assert real_rate.reason.value == "derived_snapshot_unavailable"
    assert not real_rate.history


def test_derived_history_ignores_references_outside_ten_year_window() -> None:
    snapshot = _real_rate_snapshot()
    old_result = replace(snapshot.result, reference_date="2014-09-25")
    old_snapshot = replace(snapshot, result=old_result)

    components = build_component_series(
        MacroScoreSources((), (), (old_snapshot,)),
        MacroScoreCutoff(AS_OF, date(2026, 9, 1)),
    )
    real_rate = next(item for item in components if item.key is MacroComponentKey("juro_real_ex_ante_1y"))

    assert real_rate.current is not None
    assert real_rate.current.reference_period == date(2014, 9, 25)
    assert not real_rate.history


def test_credit_derived_snapshot_obeys_monthly_freshness_for_current_and_history() -> None:
    snapshot = _real_rate_snapshot()
    stale_result = replace(
        snapshot.result,
        key="spread_credito_pf_selic",
        reference_date="2026-06-25",
        unit="pp",
        formula="taxa_media_credito_pf - selic_meta",
    )
    stale_snapshot = replace(snapshot, result=stale_result)

    components = build_component_series(
        MacroScoreSources((), (), (stale_snapshot,)),
        MacroScoreCutoff(AS_OF, date(2026, 9, 1)),
    )
    spread = next(item for item in components if item.key is MacroComponentKey("spread_credito_pf_selic"))

    assert spread.current is None
    assert spread.reason.value == "stale_input"
    assert not spread.history
