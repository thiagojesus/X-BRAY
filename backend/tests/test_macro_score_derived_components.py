from dataclasses import replace
from datetime import date, datetime, timedelta, timezone

from derived.models import DerivedInput, DerivedResult, SnapshotOrigin
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
    assert real_rate.current.available_at == production.created_at
    assert real_rate.current.reference_period == date(2026, 9, 25)
    assert len(real_rate.history) == 1
    assert real_rate.history[0].input_fingerprint == real_rate.current.input_fingerprint
