from collections.abc import Callable
from datetime import datetime, timezone

from derived.models import SnapshotOrigin
from macro_score.methodology import COMPONENTS, Dimension
from macro_score.refresh import refresh_macro_score
from macro_score.repository import query_latest_macro_score

Check = Callable[[str, bool, str], None]


def run_macro_score_smoke(check: Check) -> None:
    as_of = datetime(2026, 10, 1, 1, 30, tzinfo=timezone.utc)
    cycle_id = "xbr601-postgres-smoke"

    inserted = refresh_macro_score(as_of, cycle_id)
    snapshot = query_latest_macro_score()
    check("macro score coordinator inserts production snapshot", inserted == 1, f"inserted={inserted}")
    check("macro score coordinator exposes production snapshot", snapshot is not None, str(snapshot is not None))
    if snapshot is None:
        return

    payload = snapshot.payload
    component_keys = {str(component.key) for component in COMPONENTS}
    dimension_keys = {dimension.value for dimension in Dimension}
    check("macro score coordinator keeps the cutoff", payload["as_of"] == as_of.isoformat(), payload["as_of"])
    check("macro score coordinator uses Sao Paulo score month", payload["score_month"] == "2026-09", payload["score_month"])
    check("macro score coordinator keeps cycle identity", snapshot.calculation_cycle_id == cycle_id, snapshot.calculation_cycle_id)
    check("macro score coordinator records production origin", snapshot.origin is SnapshotOrigin.PRODUCTION_REFRESH, snapshot.origin.value)
    check("macro score coordinator serializes complete component inventory", set(payload["components"]) == component_keys, str(len(payload["components"])))
    check("macro score coordinator serializes complete dimension inventory", set(payload["dimensions"]) == dimension_keys, str(len(payload["dimensions"])))
    check("empty source database yields unavailable score", payload["score"] is None and payload["valid_dimensions"] == 0, str(payload["score"]))
    check("empty source database fabricates no component inputs", all(not item["inputs"] for item in payload["components"].values()), "")

    same_cycle_retry = refresh_macro_score(as_of, cycle_id)
    other_cycle_retry = refresh_macro_score(as_of, f"{cycle_id}-retry")
    latest_after_retry = query_latest_macro_score()
    check("same-cycle macro score retry is idempotent", same_cycle_retry == 0, f"inserted={same_cycle_retry}")
    check("new-cycle identical macro score retry is idempotent", other_cycle_retry == 0, f"inserted={other_cycle_retry}")
    check("idempotent retries preserve first production snapshot", latest_after_retry == snapshot, str(latest_after_retry == snapshot))
