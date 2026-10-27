from types import SimpleNamespace
from unittest.mock import Mock

from derived.models import SnapshotOrigin
from macro_score.methodology import COMPONENTS, Dimension


def test_postgres_macro_score_smoke_assertions_are_testable_without_database(monkeypatch) -> None:
    import smoke_pg_macro_score as smoke

    payload = {
        "as_of": "2026-10-01T01:30:00+00:00",
        "score_month": "2026-09",
        "components": {
            str(component.key): {"inputs": []} for component in COMPONENTS
        },
        "dimensions": {dimension.value: {} for dimension in Dimension},
        "score": None,
        "valid_dimensions": 0,
    }
    snapshot = SimpleNamespace(
        payload=payload,
        calculation_cycle_id="xbr601-postgres-smoke",
        origin=SnapshotOrigin.PRODUCTION_REFRESH,
    )
    refresh = Mock(side_effect=[1, 0, 0])
    query = Mock(side_effect=[snapshot, snapshot])
    monkeypatch.setattr(smoke, "refresh_macro_score", refresh)
    monkeypatch.setattr(smoke, "query_latest_macro_score", query)
    checks: list[tuple[str, bool]] = []

    smoke.run_macro_score_smoke(
        lambda name, passed, _detail: checks.append((name, passed))
    )

    assert checks and all(passed for _name, passed in checks)
    assert refresh.call_count == 3
    assert query.call_count == 2


def test_postgres_macro_score_smoke_stops_cleanly_without_snapshot(monkeypatch) -> None:
    import smoke_pg_macro_score as smoke

    monkeypatch.setattr(smoke, "refresh_macro_score", lambda *_args: 0)
    monkeypatch.setattr(smoke, "query_latest_macro_score", lambda: None)
    checks: list[tuple[str, bool]] = []

    smoke.run_macro_score_smoke(
        lambda name, passed, _detail: checks.append((name, passed))
    )

    assert checks == [
        ("macro score coordinator inserts production snapshot", False),
        ("macro score coordinator exposes production snapshot", False),
    ]
