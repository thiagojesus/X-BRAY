from collections.abc import Mapping
from dataclasses import dataclass, replace
from datetime import date, datetime
from types import MappingProxyType
from typing import Final

from derived.models import DerivedInput, SnapshotOrigin
from derived.repository import DerivedSnapshot
from indicator_catalog import INDICATOR_CATALOG, IndicatorKey
from macro_score.component_points import ComponentCalculation, component_point
from macro_score.inputs import (
    MacroScoreCutoff,
    ScoreSourcePolicy,
    current_reference_is_fresh,
    historical_month_window,
    score_cutoff,
)
from macro_score.methodology import ComponentSpec, MacroComponentKey
from macro_score.models import (
    ComponentPoint,
    ComponentSeries,
    ComponentUnavailableReason,
)
from macro_score.sources import MacroScoreSources

_SNAPSHOT_KEYS: Final[Mapping[MacroComponentKey, str]] = MappingProxyType({
    MacroComponentKey("juro_real_ex_ante_1y"): "juro_real_ex_ante_1y",
    MacroComponentKey("inclinacao_di_5y_1y"): "inclinacao_di_5y_1y",
    MacroComponentKey("spread_credito_pf_selic"): "spread_credito_pf_selic",
    MacroComponentKey("cobertura_externa"): "cobertura_externa",
})
_ANCHOR_KEYS: Final[Mapping[MacroComponentKey, str | None]] = MappingProxyType({
    MacroComponentKey("juro_real_ex_ante_1y"): None,
    MacroComponentKey("inclinacao_di_5y_1y"): None,
    MacroComponentKey("spread_credito_pf_selic"): "taxa_media_credito_pf",
    MacroComponentKey("cobertura_externa"): "idp_12m_pib",
})


@dataclass(frozen=True, slots=True)
class _AvailableDerivedSnapshot:
    snapshot: DerivedSnapshot
    reference_date: date
    value: float


def _snapshot_is_forward(
    snapshot: DerivedSnapshot,
    cutoff: MacroScoreCutoff,
) -> bool:
    return (
        snapshot.origin is SnapshotOrigin.PRODUCTION_REFRESH
        and snapshot.result.as_of <= snapshot.created_at <= cutoff.as_of
        and all(item.available_at <= snapshot.result.as_of for item in snapshot.result.inputs)
    )


def _snapshot_is_fresh(
    spec: ComponentSpec,
    reference_date: date,
    as_of: datetime,
) -> bool:
    anchor_key = _ANCHOR_KEYS[spec.key]
    if anchor_key is None:
        return True
    anchor = INDICATOR_CATALOG[IndicatorKey(anchor_key)]
    policy = ScoreSourcePolicy(
        anchor.provider.value,
        str(anchor.key),
        anchor.frequency,
        anchor.freshness_days,
    )
    return current_reference_is_fresh(reference_date, score_cutoff(as_of), policy)


def _snapshot_inputs(snapshot: DerivedSnapshot) -> tuple[DerivedInput, ...]:
    return tuple(
        replace(
            item,
            metadata=(*item.metadata, ("derived_snapshot_inputs_hash", snapshot.inputs_hash)),
        )
        for item in snapshot.result.inputs
    )


def _point_from_snapshot(
    spec: ComponentSpec,
    available: _AvailableDerivedSnapshot,
) -> ComponentPoint:
    snapshot = available.snapshot
    inputs = _snapshot_inputs(snapshot)
    return component_point(
        spec,
        ComponentCalculation(
            value=available.value,
            reference_period=available.reference_date,
            available_at=snapshot.created_at,
            inputs=inputs,
        ),
    )


def _history_points(
    spec: ComponentSpec,
    snapshots: tuple[DerivedSnapshot, ...],
    cutoff: MacroScoreCutoff,
) -> tuple[ComponentPoint, ...]:
    window = frozenset(historical_month_window(cutoff.score_month))
    latest_by_month: dict[date, tuple[DerivedSnapshot, date]] = {}
    for snapshot in snapshots:
        if not _snapshot_is_forward(snapshot, cutoff):
            continue
        raw_reference = snapshot.result.reference_date
        if raw_reference is None:
            continue
        reference_date = date.fromisoformat(raw_reference)
        month = date(reference_date.year, reference_date.month, 1)
        if month not in window:
            continue
        latest = latest_by_month.get(month)
        identity = (
            reference_date,
            snapshot.result.as_of,
            snapshot.created_at,
            snapshot.inputs_hash,
        )
        if latest is None or identity > (
            latest[1],
            latest[0].result.as_of,
            latest[0].created_at,
            latest[0].inputs_hash,
        ):
            latest_by_month[month] = (snapshot, reference_date)
    points: list[ComponentPoint] = []
    for month in sorted(latest_by_month):
        snapshot, reference_date = latest_by_month[month]
        if snapshot.result.value is None or not _snapshot_is_fresh(
            spec,
            reference_date,
            snapshot.result.as_of,
        ):
            continue
        points.append(_point_from_snapshot(
            spec,
            _AvailableDerivedSnapshot(snapshot, reference_date, snapshot.result.value),
        ))
    return tuple(points)


def build_derived_snapshot_series(
    spec: ComponentSpec,
    sources: MacroScoreSources,
    cutoff: MacroScoreCutoff,
) -> ComponentSeries:
    snapshot_key = _SNAPSHOT_KEYS[spec.key]
    snapshots = tuple(
        snapshot
        for snapshot in sources.derived_snapshots
        if snapshot.result.key == snapshot_key and _snapshot_is_forward(snapshot, cutoff)
    )
    history = _history_points(spec, snapshots, cutoff)
    if not snapshots:
        return ComponentSeries(
            spec.key,
            None,
            history,
            ComponentUnavailableReason.DERIVED_SNAPSHOT_MISSING,
        )

    latest = max(
        snapshots,
        key=lambda item: (item.result.as_of, item.created_at, item.inputs_hash),
    )
    reference = latest.result.reference_date
    if latest.result.value is None or reference is None:
        return ComponentSeries(
            spec.key,
            None,
            history,
            ComponentUnavailableReason.DERIVED_SNAPSHOT_UNAVAILABLE,
            upstream_reason=latest.result.reason,
        )
    reference_date = date.fromisoformat(reference)
    if not _snapshot_is_fresh(spec, reference_date, cutoff.as_of):
        return ComponentSeries(spec.key, None, history, ComponentUnavailableReason.STALE_INPUT)
    return ComponentSeries(
        spec.key,
        _point_from_snapshot(
            spec,
            _AvailableDerivedSnapshot(latest, reference_date, latest.result.value),
        ),
        history,
        None,
    )
