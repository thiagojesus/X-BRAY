from datetime import datetime, timezone

from derived.models import SnapshotOrigin
from macro_score.calculator import aggregate_macro_score
from macro_score.component_inputs import build_component_series
from macro_score.inputs import score_cutoff
from macro_score.normalization import normalize_component
from macro_score.repository import insert_macro_score_snapshot
from macro_score.retrospective import overlay_retrospective_history
from macro_score.sources import load_macro_score_sources


def refresh_macro_score(as_of: datetime, calculation_cycle_id: str) -> int:
    cutoff = score_cutoff(as_of)
    sources = load_macro_score_sources(as_of=cutoff.as_of)
    series = overlay_retrospective_history(
        build_component_series(sources, cutoff, calculation_cycle_id),
        as_of=cutoff.as_of,
    )
    components = tuple(normalize_component(item) for item in series)
    result = aggregate_macro_score(components, cutoff, datetime.now(timezone.utc))
    inserted = insert_macro_score_snapshot(
        result,
        calculation_cycle_id=calculation_cycle_id,
        origin=SnapshotOrigin.PRODUCTION_REFRESH,
    )
    return int(inserted)
