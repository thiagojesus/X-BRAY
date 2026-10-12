from datetime import date, datetime, timedelta, timezone
from pathlib import Path

import pytest


def _require_component_builder() -> None:
    component_path = Path(__file__).parents[1] / "macro_score" / "component_inputs.py"
    if not component_path.is_file():
        pytest.fail("[RED XBR-601] Macro Score component input composition is missing")


def test_red_xbr_601_yoy_component_uses_exact_twelve_month_pair() -> None:
    _require_component_builder()
    from macro_score.component_inputs import build_component_series
    from macro_score.inputs import MacroScoreCutoff, ScoreVintage
    from macro_score.sources import MacroScoreSources

    as_of = datetime(2026, 9, 15, 12, tzinfo=timezone.utc)
    cutoff = MacroScoreCutoff(as_of, as_of.date().replace(day=1))
    available_at = datetime(2026, 9, 10, 12, tzinfo=timezone.utc)
    vintages = (
        ScoreVintage(
            provider="bcb_sgs",
            series_key="ibc_br_sa",
            reference_period=date(2025, 8, 31),
            value=100.0,
            unit="index",
            source="fixture",
            available_at=available_at,
            first_seen_at=available_at,
            payload_hash="ibc-prior",
        ),
        ScoreVintage(
            provider="bcb_sgs",
            series_key="ibc_br_sa",
            reference_period=date(2026, 8, 31),
            value=110.0,
            unit="index",
            source="fixture",
            available_at=available_at,
            first_seen_at=available_at,
            payload_hash="ibc-current",
        ),
    )
    sources = MacroScoreSources(vintages=vintages, target_observations=(), derived_snapshots=())

    components = {str(item.key): item for item in build_component_series(sources, cutoff)}

    assert components["ibc_br_sa_yoy"].current is not None
    assert components["ibc_br_sa_yoy"].current.value == pytest.approx(10.0)
    assert components["ibc_br_sa_yoy"].current.unit.value == "pct"


def test_dbgg_change_is_twelve_month_level_difference_in_percentage_points() -> None:
    _require_component_builder()
    from macro_score.component_inputs import build_component_series
    from macro_score.inputs import MacroScoreCutoff, ScoreVintage
    from macro_score.sources import MacroScoreSources

    as_of = datetime(2026, 9, 15, 12, tzinfo=timezone.utc)
    cutoff = MacroScoreCutoff(as_of, as_of.date().replace(day=1))
    available_at = datetime(2026, 9, 10, 12, tzinfo=timezone.utc)
    vintages = (
        ScoreVintage(
            provider="bcb_sgs",
            series_key="dbgg_pib",
            reference_period=date(2025, 8, 31),
            value=75.0,
            unit="pct_gdp",
            source="fixture",
            available_at=available_at,
            first_seen_at=available_at,
            payload_hash="dbgg-prior",
        ),
        ScoreVintage(
            provider="bcb_sgs",
            series_key="dbgg_pib",
            reference_period=date(2026, 8, 31),
            value=80.0,
            unit="pct_gdp",
            source="fixture",
            available_at=available_at,
            first_seen_at=available_at,
            payload_hash="dbgg-current",
        ),
    )
    sources = MacroScoreSources(vintages=vintages, target_observations=(), derived_snapshots=())

    components = {str(item.key): item for item in build_component_series(sources, cutoff)}

    assert components["dbgg_yoy_pp"].current is not None
    assert components["dbgg_yoy_pp"].current.value == 5.0
    assert components["dbgg_yoy_pp"].current.unit.value == "pp"


def test_yoy_component_is_unavailable_without_exact_twelve_month_reference() -> None:
    _require_component_builder()
    from macro_score.component_inputs import build_component_series
    from macro_score.inputs import MacroScoreCutoff, ScoreVintage
    from macro_score.models import ComponentUnavailableReason
    from macro_score.sources import MacroScoreSources

    as_of = datetime(2026, 9, 15, 12, tzinfo=timezone.utc)
    cutoff = MacroScoreCutoff(as_of, as_of.date().replace(day=1))
    available_at = datetime(2026, 9, 10, 12, tzinfo=timezone.utc)
    vintages = (
        ScoreVintage(
            provider="bcb_sgs",
            series_key="ibc_br_sa",
            reference_period=date(2025, 7, 31),
            value=100.0,
            unit="index",
            source="fixture",
            available_at=available_at,
            first_seen_at=available_at,
            payload_hash="wrong-prior-month",
        ),
        ScoreVintage(
            provider="bcb_sgs",
            series_key="ibc_br_sa",
            reference_period=date(2026, 8, 31),
            value=110.0,
            unit="index",
            source="fixture",
            available_at=available_at,
            first_seen_at=available_at,
            payload_hash="ibc-current",
        ),
    )
    sources = MacroScoreSources(vintages=vintages, target_observations=(), derived_snapshots=())

    components = {str(item.key): item for item in build_component_series(sources, cutoff)}

    assert components["ibc_br_sa_yoy"].current is None
    assert components["ibc_br_sa_yoy"].reason is ComponentUnavailableReason.HISTORICAL_LAG_MISSING


def test_component_builder_emits_all_eighteen_independent_missing_states() -> None:
    from macro_score.component_inputs import build_component_series
    from macro_score.inputs import MacroScoreCutoff
    from macro_score.methodology import COMPONENTS
    from macro_score.sources import MacroScoreSources

    cutoff = MacroScoreCutoff(
        datetime(2026, 9, 15, 12, tzinfo=timezone.utc),
        date(2026, 9, 1),
    )
    components = build_component_series(MacroScoreSources((), (), ()), cutoff)

    assert len(components) == 18
    assert {item.key for item in components} == {spec.key for spec in COMPONENTS}
    assert all(item.current is None and item.reason is not None for item in components)


def test_future_sgs_revision_reduces_yoy_history_without_backdating_availability() -> None:
    from macro_score.component_inputs import build_component_series
    from macro_score.inputs import MacroScoreCutoff, ScoreVintage
    from macro_score.methodology import MacroComponentKey
    from macro_score.models import ComponentUnavailableReason
    from macro_score.normalization import normalize_component
    from macro_score.sources import MacroScoreSources

    as_of = datetime(2026, 9, 15, 12, tzinfo=timezone.utc)
    available_at = as_of - timedelta(hours=1)
    first_month = date(2023, 9, 1)
    vintages: list[ScoreVintage] = []
    for index in range(36):
        month_index = first_month.year * 12 + first_month.month - 1 + index
        reference_period = date(month_index // 12, month_index % 12 + 1, 1)
        observed_at = as_of + timedelta(seconds=1) if index == 35 else available_at
        vintages.append(ScoreVintage(
            provider="bcb_sgs",
            series_key="ibc_br_sa",
            reference_period=reference_period,
            value=100.0 + index,
            unit="index",
            source="fixture SGS",
            available_at=observed_at,
            first_seen_at=observed_at,
            payload_hash=f"ibc-vintage-{index}",
        ))
    sources = MacroScoreSources(tuple(vintages), (), ())

    components = build_component_series(
        sources,
        MacroScoreCutoff(as_of, date(2026, 9, 1)),
    )
    ibc_yoy = next(item for item in components if item.key is MacroComponentKey("ibc_br_sa_yoy"))
    normalized = normalize_component(ibc_yoy)

    assert ibc_yoy.current is not None
    assert ibc_yoy.current.reference_period == date(2026, 7, 1)
    assert len(ibc_yoy.history) == 23
    assert normalized.percentile is None
    assert normalized.reason is ComponentUnavailableReason.INSUFFICIENT_HISTORY
