from dataclasses import replace
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from db import store
from derived.models import SnapshotOrigin
from tests.macro_score_support import build_macro_score_result
from tests.real_rate_support import isolated_database

pytestmark = pytest.mark.usefixtures(isolated_database.__name__)


def _require_repository_module() -> None:
    repository_path = Path(__file__).parents[1] / "macro_score" / "repository.py"
    if not repository_path.is_file():
        pytest.fail("[RED XBR-601] Macro Score immutable repository is missing")


def test_red_xbr_601_macro_score_snapshot_schema_is_migrated() -> None:
    tables = store.query_rows("SELECT name FROM sqlite_master WHERE type = ?", ("table",))
    assert "macro_score_snapshots" in {str(row["name"]) for row in tables}, (
        "[RED XBR-601] macro_score_snapshots migration is not registered"
    )

    columns = store.query_rows("PRAGMA table_info(macro_score_snapshots)")
    assert {str(row["name"]) for row in columns} >= {
        "score_month",
        "methodology_version",
        "as_of",
        "calculated_at",
        "inputs_hash",
        "payload_json",
        "calculation_cycle_id",
        "origin",
        "created_at",
    }


def test_red_xbr_601_repository_keeps_revisions_immutable_and_latest_per_month() -> None:
    _require_repository_module()
    from macro_score.repository import (
        insert_macro_score_snapshot,
        query_latest_macro_score,
        query_macro_score_history,
    )

    as_of = datetime(2026, 8, 31, 15, tzinfo=timezone.utc)
    calculated_at = as_of + timedelta(seconds=10)
    created_at = calculated_at + timedelta(seconds=1)
    first_result = build_macro_score_result(as_of, calculated_at)

    first_insert = insert_macro_score_snapshot(
        first_result,
        calculation_cycle_id="cycle-first",
        origin=SnapshotOrigin.PRODUCTION_REFRESH,
        created_at=created_at,
    )
    duplicate_insert = insert_macro_score_snapshot(
        replace(first_result, calculated_at=calculated_at + timedelta(seconds=5)),
        calculation_cycle_id="cycle-duplicate",
        origin=SnapshotOrigin.PRODUCTION_REFRESH,
        created_at=created_at + timedelta(seconds=10),
    )
    revised_result = build_macro_score_result(
        as_of,
        calculated_at + timedelta(days=1),
        current_shift=0.5,
    )
    revision_insert = insert_macro_score_snapshot(
        revised_result,
        calculation_cycle_id="cycle-revision",
        origin=SnapshotOrigin.PRODUCTION_REFRESH,
        created_at=created_at + timedelta(days=1),
    )
    snapshots = query_macro_score_history("2026-08", "2026-08")
    stored_rows = store.query_rows(
        "SELECT calculation_cycle_id, created_at FROM macro_score_snapshots "
        "ORDER BY created_at ASC"
    )
    latest = query_latest_macro_score()

    assert first_insert is True
    assert duplicate_insert is False
    assert revision_insert is True
    assert [row["calculation_cycle_id"] for row in stored_rows] == [
        "cycle-first",
        "cycle-revision",
    ]
    assert stored_rows[0]["created_at"] == created_at.isoformat()
    assert len(snapshots) == 1
    assert latest is not None
    assert latest.payload["calculation_cycle_id"] == "cycle-revision"
    assert latest.inputs_hash == revised_result.input_fingerprint
    assert snapshots[0] == latest


def test_macro_score_repository_excludes_fixture_origin_and_validates_month_range() -> None:
    _require_repository_module()
    from macro_score.repository import (
        InvalidMacroScoreRangeError,
        insert_macro_score_snapshot,
        query_latest_macro_score,
        query_macro_score_history,
    )

    as_of = datetime(2026, 9, 30, 15, tzinfo=timezone.utc)
    result = build_macro_score_result(as_of, as_of + timedelta(seconds=5))
    insert_macro_score_snapshot(
        result,
        calculation_cycle_id="synthetic-cycle",
        origin=SnapshotOrigin.TEST_FIXTURE,
    )

    with pytest.raises(InvalidMacroScoreRangeError):
        query_macro_score_history("2026-13", "2026-14")
    assert query_latest_macro_score() is None


def test_macro_score_history_returns_one_latest_production_snapshot_per_month() -> None:
    _require_repository_module()
    from macro_score.repository import (
        insert_macro_score_snapshot,
        query_macro_score_history,
    )

    august = datetime(2026, 8, 31, 15, tzinfo=timezone.utc)
    september = datetime(2026, 9, 30, 15, tzinfo=timezone.utc)
    for timestamp, cycle in ((august, "august"), (september, "september")):
        result = build_macro_score_result(timestamp, timestamp + timedelta(seconds=5))
        insert_macro_score_snapshot(
            result,
            calculation_cycle_id=cycle,
            origin=SnapshotOrigin.PRODUCTION_REFRESH,
        )

    history = query_macro_score_history("2026-08", "2026-09")

    assert [snapshot.payload["score_month"] for snapshot in history] == ["2026-08", "2026-09"]
