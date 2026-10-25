from collections.abc import Iterator
from datetime import date, datetime, timezone
from pathlib import Path
from unittest.mock import patch

import pytest
from fastapi.testclient import TestClient


@pytest.fixture
def isolated_database(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    from db import store

    if store._conn is not None:
        store._conn.close()
    store._conn = None
    monkeypatch.setattr(store, "DB_PATH", tmp_path / "refresh-status.db")
    store.init_db()
    yield
    if store._conn is not None:
        store._conn.close()
    store._conn = None


def test_red_xbr_003_refresh_status_starts_as_never_run(isolated_database: None) -> None:
    # Given
    from main import app

    # When
    with TestClient(app, raise_server_exceptions=False) as client:
        response = client.get("/api/refresh-status")

    # Then
    assert response.status_code == 200
    assert response.json()["overall_status"] == "never_run"
    assert response.json()["tasks"] == []


def test_refresh_status_reports_healthy_task_with_normalized_metrics() -> None:
    from refresh import refresh_status_payload

    rows: list[dict[str, object]] = [{
        "task": "sgs_juros",
        "provider": "bcb_sgs",
        "last_attempt": "2026-09-12T10:00:00+00:00",
        "last_success": "2026-09-12T10:00:00",
        "status": "ok",
        "series_expected": "3",
        "series_updated": 3,
        "record_count": 12.0,
        "duration_ms": None,
        "error": None,
    }]
    with (
        patch("refresh_reporting.list_refresh_tasks", return_value=rows),
        patch("refresh_reporting.read_latest_run", return_value=None),
    ):
        payload = refresh_status_payload(datetime(2026, 9, 12, 12, tzinfo=timezone.utc))

    assert payload["overall_status"] == "ok"
    assert payload["tasks"] == [{
        "task": "sgs_juros",
        "provider": "bcb_sgs",
        "last_attempt": "2026-09-12T10:00:00+00:00",
        "last_success": "2026-09-12T10:00:00",
        "status": "ok",
        "series_expected": 3,
        "series_updated": 3,
        "record_count": 12,
        "duration_ms": 0,
        "freshness": "ok",
        "sla_deadline": "2026-09-13T16:00:00+00:00",
        "error_code": None,
    }]


def test_refresh_status_distinguishes_stale_and_never_successful_tasks() -> None:
    from refresh import refresh_status_payload

    base = {
        "provider": "provider",
        "last_attempt": None,
        "status": "error",
        "series_expected": 1,
        "series_updated": 0,
        "record_count": 0,
        "duration_ms": 1,
        "error": "TimeoutError",
    }
    rows = [
        {**base, "task": "stale", "last_success": "2026-09-10T00:00:00Z"},
        {**base, "task": "never", "last_success": None},
    ]
    with (
        patch("refresh_reporting.list_refresh_tasks", return_value=rows),
        patch("refresh_reporting.read_latest_run", return_value=None),
    ):
        payload = refresh_status_payload(datetime(2026, 9, 12, 12, tzinfo=timezone.utc))

    assert payload["overall_status"] == "partial"
    assert [task["freshness"] for task in payload["tasks"]] == ["stale", "never"]
    assert all(task["error_code"] == "TimeoutError" for task in payload["tasks"])


def _persist_refresh_run(
    *,
    run_id: str,
    manifest_version: str,
    tasks: list[str],
    failed_task: str | None = None,
    pending_task: str | None = None,
    started_at: datetime | None = None,
) -> None:
    from db.refresh_run_finalize import finalize_and_release_run
    from db.refresh_run_models import (
        CycleId,
        RefreshRunFinalization,
        RefreshRunFinalState,
        RefreshRunStart,
        RefreshWorkItemSpec,
        RunId,
        WorkItemKey,
    )
    from db.refresh_run_sql import execute, transaction
    from db.refresh_runs import create_or_resume_run

    started = started_at or datetime(2026, 10, 24, 17, 0, tzinfo=timezone.utc)
    run_key = RunId(run_id)
    now = started.isoformat()
    create_or_resume_run(RefreshRunStart(
        run_id=run_key,
        cycle_id=CycleId(f"cycle-{run_id}"),
        force=False,
        manifest_version=manifest_version,
        derived_cutoff=None,
        macro_cutoff=None,
        items=tuple(
            RefreshWorkItemSpec(WorkItemKey(task), task, order)
            for order, task in enumerate(tasks)
        ),
        now=now,
        lease_expires_at="2026-10-25T17:00:00+00:00",
    ))
    with transaction() as connection:
        for task in tasks:
            failed = task == failed_task
            pending = task == pending_task
            execute(
                connection,
                "UPDATE refresh_work_items SET status = ?, attempt = ?, claimed_at = ?, "
                "finished_at = ?, updated_at = ?, series_updated = ?, record_count = ?, error_code = ? "
                "WHERE run_id = ? AND item_key = ?",
                (
                    "failed" if failed else "pending" if pending else "succeeded",
                    2 if failed else 0 if pending else 1,
                    None if pending else now,
                    None if pending else "2026-10-24T17:01:00+00:00",
                    now if pending else "2026-10-24T17:01:00+00:00",
                    0 if failed or pending else 1,
                    0 if failed or pending else 2,
                    "ProviderTimeout" if failed else None,
                    run_key,
                    task,
                ),
            )
    if failed_task is None and pending_task is None:
        assert finalize_and_release_run(RefreshRunFinalization(
            run_id=run_key,
            state=RefreshRunFinalState.SUCCEEDED,
            final_result='{"status":"ok"}',
            finished_at="2026-10-24T17:02:00+00:00",
        ))


def _ipca_window_tasks() -> list[str]:
    from config import IPCA_GROUPS

    return [
        f"sgs_ipca_grupos_{code}_{window}"
        for code in IPCA_GROUPS.values()
        for window in range(4)
    ]


def _interest_window_tasks(end_date: str = "2026-10-24") -> list[str]:
    from datafetchers.bcb_sgs import sgs_annual_windows

    names = ["sgs_juros_other"]
    for code in (432, 226):
        for start_text, end_text in sgs_annual_windows("2015-01-01", end_date):
            start = date(int(start_text[6:]), int(start_text[3:5]), int(start_text[:2])).isoformat()
            end = date(int(end_text[6:]), int(end_text[3:5]), int(end_text[:2])).isoformat()
            names.append(f"sgs_juros_w{code}_{start}_{end}")
    return names


def test_refresh_status_aggregates_v4_to_four_logical_sgs_series(isolated_database: None) -> None:
    from refresh import refresh_status_payload

    _persist_refresh_run(
        run_id="refresh-v4-interest-complete",
        manifest_version="daily-refresh-v4",
        tasks=_interest_window_tasks(),
    )
    payload = refresh_status_payload(datetime(2026, 10, 24, 18, tzinfo=timezone.utc))
    interest = next(task for task in payload["tasks"] if task["task"] == "sgs_juros")
    assert interest["series_expected"] == 4
    assert interest["series_updated"] == 4
    assert interest["status"] == "ok"


def test_refresh_status_fails_closed_when_v4_annual_window_is_missing(isolated_database: None) -> None:
    from refresh import refresh_status_payload

    tasks = _interest_window_tasks()
    _persist_refresh_run(
        run_id="refresh-v4-interest-missing-window",
        manifest_version="daily-refresh-v4",
        tasks=tasks[:-1],
    )
    payload = refresh_status_payload(datetime(2026, 10, 24, 18, tzinfo=timezone.utc))
    interest = next(task for task in payload["tasks"] if task["task"] == "sgs_juros")
    assert interest["series_expected"] == 4
    assert interest["status"] == "partial"
    assert interest["error_code"] == "IncompleteSGSInterestManifest"


def test_refresh_status_uses_completed_v2_windows_instead_of_stale_legacy_aggregate(
    isolated_database: None,
) -> None:
    from db.refresh_run_models import RefreshWorkItemStatus
    from db.refresh_runs import read_latest_run, read_run
    from db.store import (
        list_refresh_tasks,
        record_refresh_task_finish,
        record_refresh_task_start,
    )
    from main import app

    record_refresh_task_start(
        "sgs_ipca_grupos", "bcb_sgs", 9, "2026-10-24T02:33:00+00:00"
    )
    record_refresh_task_finish(
        "sgs_ipca_grupos", "error", 0, 0, 12000, "JSONDecodeError",
        "2026-10-24T02:33:12+00:00",
    )
    _persist_refresh_run(
        run_id="refresh-v1-old",
        manifest_version="daily-refresh-v1",
        tasks=["sgs_ipca_grupos"],
        started_at=datetime(2026, 10, 24, 16, 0, tzinfo=timezone.utc),
    )
    tasks = [*_ipca_window_tasks(), *(f"other_task_{index}" for index in range(21))]
    _persist_refresh_run(
        run_id="refresh-v2-complete",
        manifest_version="daily-refresh-v2",
        tasks=tasks,
    )

    with TestClient(app) as client:
        response = client.get("/api/refresh-status")
    payload = response.json()
    group = next(task for task in payload["tasks"] if task["task"] == "sgs_ipca_grupos")
    snapshot = read_latest_run()
    old_snapshot = read_run("refresh-v1-old")

    assert response.status_code == 200
    assert len(tasks) == 57
    assert snapshot is not None and len(snapshot.items) == 57
    assert snapshot.run.state.value == "succeeded"
    assert all(item.status is RefreshWorkItemStatus.SUCCEEDED for item in snapshot.items)
    assert group["status"] == "ok"
    assert group["series_expected"] == 9
    assert group["series_updated"] == 9
    assert group["record_count"] == 72
    assert group["error_code"] is None
    assert payload["overall_status"] == "ok"
    assert old_snapshot is not None and old_snapshot.run.state.value == "succeeded"
    legacy = next(row for row in list_refresh_tasks() if row["task"] == "sgs_ipca_grupos")
    assert legacy["status"] == "error"
    assert legacy["error"] == "JSONDecodeError"


def test_refresh_status_aggregates_v3_without_retired_legacy_task(
    isolated_database: None,
) -> None:
    from db.refresh_run_models import RefreshWorkItemStatus
    from db.refresh_runs import read_latest_run
    from main import app

    tasks = [*_ipca_window_tasks(), *(f"other_task_{index}" for index in range(20))]
    _persist_refresh_run(
        run_id="refresh-v3-no-retired-task",
        manifest_version="daily-refresh-v3",
        tasks=tasks,
    )

    with TestClient(app) as client:
        response = client.get("/api/refresh-status")

    snapshot = read_latest_run()
    payload = response.json()
    group = next(task for task in payload["tasks"] if task["task"] == "sgs_ipca_grupos")
    assert response.status_code == 200
    assert len(tasks) == 56
    assert snapshot is not None and snapshot.run.manifest_version == "daily-refresh-v3"
    assert all(item.task != "polymarket" for item in snapshot.items)
    assert all(item.status is RefreshWorkItemStatus.SUCCEEDED for item in snapshot.items)
    assert group["status"] == "ok"
    assert payload["overall_status"] == "ok"


def test_refresh_status_keeps_v2_window_failures_visible(
    isolated_database: None,
) -> None:
    from db.store import record_refresh_task_finish, record_refresh_task_start
    from refresh import refresh_status_payload

    record_refresh_task_start("sgs_ipca_grupos", "bcb_sgs", 9, "2026-10-23T02:00:00+00:00")
    record_refresh_task_finish(
        "sgs_ipca_grupos", "error", 0, 0, 1, "OldFailure", "2026-10-23T02:00:01+00:00"
    )
    tasks = _ipca_window_tasks()
    _persist_refresh_run(
        run_id="refresh-v2-failed-window",
        manifest_version="daily-refresh-v2",
        tasks=tasks,
        failed_task=tasks[0],
    )

    payload = refresh_status_payload(datetime(2026, 10, 24, 17, 3, tzinfo=timezone.utc))
    group = next(task for task in payload["tasks"] if task["task"] == "sgs_ipca_grupos")

    assert group["status"] == "partial"
    assert group["error_code"] == "ProviderTimeout"
    assert group["series_updated"] == 8
    assert payload["overall_status"] == "partial"


def test_refresh_status_fails_closed_for_incomplete_v2_window_manifest(
    isolated_database: None,
) -> None:
    from refresh import refresh_status_payload

    _persist_refresh_run(
        run_id="refresh-v2-missing-window",
        manifest_version="daily-refresh-v2",
        tasks=_ipca_window_tasks()[:-1],
    )

    payload = refresh_status_payload(datetime(2026, 10, 24, 17, 3, tzinfo=timezone.utc))
    group = next(task for task in payload["tasks"] if task["task"] == "sgs_ipca_grupos")

    assert group["status"] == "partial"
    assert group["error_code"] == "IncompleteIPCAGroupManifest"
    assert group["series_updated"] == 8
    assert payload["overall_status"] == "partial"


def test_refresh_status_does_not_report_ok_while_current_v2_window_is_pending(
    isolated_database: None,
) -> None:
    from refresh import refresh_status_payload

    tasks = _ipca_window_tasks()
    _persist_refresh_run(
        run_id="refresh-v2-pending-window",
        manifest_version="daily-refresh-v2",
        tasks=tasks,
        pending_task=tasks[-1],
    )

    payload = refresh_status_payload(datetime(2026, 10, 24, 17, 3, tzinfo=timezone.utc))
    group = next(task for task in payload["tasks"] if task["task"] == "sgs_ipca_grupos")

    assert group["status"] == "partial"
    assert group["series_updated"] == 8
    assert payload["overall_status"] == "partial"


def test_refresh_status_stays_partial_until_the_entire_current_v2_run_finishes(
    isolated_database: None,
) -> None:
    from refresh import refresh_status_payload

    tasks = [*_ipca_window_tasks(), *(f"other_task_{index}" for index in range(21))]
    _persist_refresh_run(
        run_id="refresh-v2-other-task-pending",
        manifest_version="daily-refresh-v2",
        tasks=tasks,
        pending_task=tasks[-1],
    )

    payload = refresh_status_payload(datetime(2026, 10, 24, 17, 3, tzinfo=timezone.utc))
    group = next(task for task in payload["tasks"] if task["task"] == "sgs_ipca_grupos")

    assert group["status"] == "ok"
    assert group["series_updated"] == 9
    assert payload["overall_status"] == "partial"


def test_refresh_status_fails_closed_for_unknown_v2_window_key(
    isolated_database: None,
) -> None:
    from refresh import refresh_status_payload

    tasks = _ipca_window_tasks()
    tasks[-1] = "sgs_ipca_grupos_9999_3"
    _persist_refresh_run(
        run_id="refresh-v2-unknown-window",
        manifest_version="daily-refresh-v2",
        tasks=tasks,
    )

    payload = refresh_status_payload(datetime(2026, 10, 24, 17, 3, tzinfo=timezone.utc))
    group = next(task for task in payload["tasks"] if task["task"] == "sgs_ipca_grupos")

    assert group["status"] == "partial"
    assert group["error_code"] == "IncompleteIPCAGroupManifest"
    assert group["series_updated"] == 8
    assert payload["overall_status"] == "partial"


def test_refresh_status_preserves_latest_v1_aggregate_semantics(
    isolated_database: None,
) -> None:
    from db.store import record_refresh_task_finish, record_refresh_task_start
    from refresh import refresh_status_payload

    record_refresh_task_start("sgs_ipca_grupos", "bcb_sgs", 9, "2026-10-24T17:00:00+00:00")
    record_refresh_task_finish(
        "sgs_ipca_grupos", "error", 0, 0, 1, "JSONDecodeError", "2026-10-24T17:00:01+00:00"
    )
    _persist_refresh_run(
        run_id="refresh-v1-latest",
        manifest_version="daily-refresh-v1",
        tasks=["sgs_ipca_grupos"],
        failed_task="sgs_ipca_grupos",
    )

    payload = refresh_status_payload(datetime(2026, 10, 24, 17, 3, tzinfo=timezone.utc))
    group = next(task for task in payload["tasks"] if task["task"] == "sgs_ipca_grupos")

    assert group["status"] == "error"
    assert group["error_code"] == "JSONDecodeError"
    assert payload["overall_status"] == "error"
