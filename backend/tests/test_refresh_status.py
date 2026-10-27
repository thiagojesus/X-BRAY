from collections.abc import Iterator
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Any
from unittest.mock import patch
from uuid import uuid4

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


def test_source_extraction_status_query_rejects_invalid_run_id() -> None:
    from db.store import source_extractions_for_refresh_run

    with pytest.raises(ValueError, match="invalid refresh run id"):
        source_extractions_for_refresh_run("")


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


def test_refresh_status_omits_retired_polymarket_but_keeps_active_provider_failure() -> None:
    from refresh import refresh_status_payload

    rows = [
        {
            "task": "polymarket", "provider": "polymarket",
            "last_attempt": "2026-10-26T02:00:00Z",
            "last_success": "2026-10-26T02:00:00Z", "status": "error",
            "series_expected": 1, "series_updated": 0, "record_count": 0,
            "duration_ms": 100, "error": "retired_source",
        },
        {
            "task": "b3_di", "provider": "b3",
            "last_attempt": "2026-10-26T02:00:00Z",
            "last_success": "2026-10-25T02:00:00Z", "status": "error",
            "series_expected": 1, "series_updated": 0, "record_count": 0,
            "duration_ms": 100, "error": "EmptyB3ResponseError",
        },
        {
            "task": "future_source", "provider": "future_provider",
            "last_attempt": "2026-10-26T02:00:00Z",
            "last_success": None, "status": "error",
            "series_expected": 1, "series_updated": 0, "record_count": 0,
            "duration_ms": 100, "error": "unknown_source_failure",
        },
    ]
    with (
        patch("refresh_reporting.list_refresh_tasks", return_value=rows),
        patch("refresh_reporting.read_latest_run", return_value=None),
    ):
        payload = refresh_status_payload(datetime(2026, 10, 26, 12, tzinfo=timezone.utc))

    assert payload["overall_status"] == "partial"
    tasks = payload["tasks"]
    assert [task["task"] for task in tasks] == ["b3_di", "future_source"]
    assert tasks[0]["error_code"] == "EmptyB3ResponseError"


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

    started = started_at or datetime.now(timezone.utc)
    run_key = RunId(run_id)
    now = started.isoformat()
    finished = (started + timedelta(minutes=1)).isoformat()
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
        lease_expires_at=(started + timedelta(days=1)).isoformat(),
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
                    None if pending else finished,
                    now if pending else finished,
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
            finished_at=finished,
        ))


def _ipca_window_tasks() -> list[str]:
    from config import IPCA_GROUPS

    return [
        f"sgs_ipca_grupos_{code}_{window}"
        for code in IPCA_GROUPS.values()
        for window in range(4)
    ]


def _interest_window_tasks(end_date: str | None = None) -> list[str]:
    from datafetchers.bcb_sgs import sgs_annual_windows

    names = ["sgs_juros_other"]
    for code in (432, 226):
        for start_text, end_text in sgs_annual_windows("2015-01-01", end_date or datetime.now(timezone.utc).date().isoformat()):
            start = date(int(start_text[6:]), int(start_text[3:5]), int(start_text[:2])).isoformat()
            end = date(int(end_text[6:]), int(end_text[3:5]), int(end_text[:2])).isoformat()
            names.append(f"sgs_juros_w{code}_{start}_{end}")
    return names


def _inflation_window_tasks(end_date: str | None = None) -> list[str]:
    from config import INFLATION
    from datafetchers.bcb_sgs import sgs_annual_windows

    cutoff = end_date or datetime.now(timezone.utc).date().isoformat()
    windows = sgs_annual_windows("2015-01-01", cutoff)
    return [
        f"sgs_inflacao_w{code}_{date(int(start[6:]), int(start[3:5]), int(start[:2])).isoformat()}_"
        f"{date(int(end[6:]), int(end[3:5]), int(end[:2])).isoformat()}"
        for code in INFLATION.values()
        for start, end in windows
    ]


def _record_sgs_window_extraction(
    *,
    run_id: str,
    task_key: str,
    code: int,
    start_date: str,
    end_date: str,
    status: str = "success",
    network_called: int = 1,
    received_count: int | None = 301,
    valid_count: int | None = 301,
    persisted_count: int | None = 301,
    source: str = "bcb_sgs",
    started_at: str | None = None,
    finished_at: str | None = None,
    qualifier_unit: str | None = None,
    identity_kind: str | None = None,
    contract_version: str | None = None,
    qualifier_overrides: dict[str, Any] | None = None,
    omit_qualifiers: tuple[str, ...] = (),
    omit_finish: bool = False,
) -> None:
    import json
    from uuid import uuid4

    from datafetchers.bcb_sgs import _sgs_window_qualifiers
    from db.store import finish_source_extraction, start_source_extraction

    now = datetime.now(timezone.utc).isoformat()
    started = started_at or now
    finished = finished_at or now
    extraction_id = str(uuid4())
    qualifiers = _sgs_window_qualifiers({
        "code": code,
        "start_date": start_date,
        "end_date": end_date,
    })
    if qualifier_unit is not None:
        qualifiers["unit"] = qualifier_unit
    if identity_kind is not None:
        qualifiers["identity_kind"] = identity_kind
    if contract_version is not None:
        qualifiers["contract_version"] = contract_version
    qualifiers.update(qualifier_overrides or {})
    for qualifier in omit_qualifiers:
        qualifiers.pop(qualifier, None)
    start_source_extraction({
        "extraction_id": extraction_id,
        "source": source,
        "indicator": str(code),
        "qualifiers_json": json.dumps(qualifiers),
        "status": "running",
        "network_called": 1,
        "started_at": started,
        "run_id": run_id,
        "task_key": task_key,
        "cycle_id": run_id,
    })
    if omit_finish:
        return
    finish_source_extraction({
        "extraction_id": extraction_id,
        "status": status,
        "network_called": network_called,
        "finished_at": finished,
        "extracted_at": finished,
        "received_count": received_count,
        "valid_count": valid_count,
        "persisted_count": persisted_count,
        "request_count": network_called,
        "retry_count": 0,
    })


def _legacy_window_projection(
    *,
    scopes: list[tuple[str, int, str, str]],
    run_id: str,
    extraction_options: dict[str, Any] | None = None,
    pending: bool = False,
    legacy_attempt_at: str = "2026-10-25T20:04:20+00:00",
) -> tuple[list[str], dict[str, object], object, object]:
    from db.refresh_runs import read_latest_run
    from db.store import (
        list_refresh_tasks,
        record_refresh_task_finish,
        record_refresh_task_start,
    )
    from main import app

    old_task = "sgs_juros_w226_2015-06-01_2017-08-30"
    record_refresh_task_start(old_task, "bcb_sgs", 1, legacy_attempt_at)
    record_refresh_task_finish(
        old_task,
        "error",
        0,
        0,
        1505,
        "InvalidSGSPayload",
        "2026-10-25T20:04:21+00:00" if "+" in legacy_attempt_at else "2026-10-25T20:04:21",
    )
    candidate_tasks = list(dict.fromkeys(
        f"sgs_{group}_w{code}_{start}_{end}"
        for group, code, start, end in scopes
    ))
    _persist_refresh_run(
        run_id=run_id,
        manifest_version="daily-refresh-v4",
        tasks=candidate_tasks,
        pending_task=candidate_tasks[0] if pending and candidate_tasks else None,
        started_at=datetime(2026, 10, 26, 21, tzinfo=timezone.utc),
    )
    options = extraction_options or {}
    for group, code, start, end in scopes:
        _record_sgs_window_extraction(
            run_id=run_id,
            task_key=f"sgs_{group}_w{code}_{start}_{end}",
            code=code,
            start_date=start,
            end_date=end,
            **options,
        )

    before = read_latest_run()
    with TestClient(app) as client:
        response = client.get("/api/refresh-status")
    after = read_latest_run()
    stored_error = next(row for row in list_refresh_tasks() if row["task"] == old_task)
    assert response.status_code == 200
    payload = response.json()
    return [row["task"] for row in payload["tasks"]], stored_error, before, after


@pytest.mark.parametrize(
    "task_name",
    [
        "sgs_wrong_w226_2026-01-01_2026-10-26",
        "sgs_juros_226_2026-01-01_2026-10-26",
        "sgs_juros_w226_not-a-date_2026-10-26",
        "sgs_juros_w226_2026-10-26_2026-01-01",
    ],
)
def test_window_identity_rejects_noncanonical_sgs_names(task_name: str) -> None:
    from refresh_reporting import _window_identity

    assert _window_identity(task_name) is None


def test_refresh_status_aggregates_v4_to_four_logical_sgs_series(isolated_database: None) -> None:
    from refresh import refresh_status_payload

    _persist_refresh_run(
        run_id="refresh-v4-interest-complete",
        manifest_version="daily-refresh-v4",
        tasks=_interest_window_tasks(),
    )
    payload = refresh_status_payload(datetime.now(timezone.utc))
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
    payload = refresh_status_payload(datetime.now(timezone.utc))
    interest = next(task for task in payload["tasks"] if task["task"] == "sgs_juros")
    assert interest["series_expected"] == 4
    assert interest["status"] == "partial"
    assert interest["error_code"] == "IncompleteSGSInterestManifest"


def test_refresh_status_aggregates_v5_inflation_children_and_records_partial_counts(
    isolated_database: None,
) -> None:
    from refresh import refresh_status_payload

    tasks = _inflation_window_tasks()
    _persist_refresh_run(
        run_id="refresh-v5-inflation-partial",
        manifest_version="daily-refresh-v5",
        tasks=tasks,
        failed_task=tasks[0],
    )

    payload = refresh_status_payload(datetime.now(timezone.utc))
    inflation = next(task for task in payload["tasks"] if task["task"] == "sgs_inflacao")

    assert inflation["series_expected"] == 7
    assert inflation["series_updated"] == 6
    assert inflation["record_count"] == 2 * (len(tasks) - 1)
    assert inflation["status"] == "partial"
    assert inflation["error_code"] == "ProviderTimeout"


def test_refresh_status_reports_all_seven_inflation_series_only_after_all_windows_succeed(
    isolated_database: None,
) -> None:
    from refresh import refresh_status_payload

    tasks = _inflation_window_tasks()
    _persist_refresh_run(
        run_id="refresh-v5-inflation-complete",
        manifest_version="daily-refresh-v5",
        tasks=tasks,
    )

    payload = refresh_status_payload(datetime.now(timezone.utc))
    inflation = next(task for task in payload["tasks"] if task["task"] == "sgs_inflacao")

    assert inflation["series_expected"] == 7
    assert inflation["series_updated"] == 7
    assert inflation["record_count"] == 2 * len(tasks)
    assert inflation["status"] == "ok"
    assert inflation["error_code"] is None


def test_refresh_status_reports_error_when_every_inflation_window_failed(
    isolated_database: None,
) -> None:
    from db.store import _execute
    from refresh import refresh_status_payload

    tasks = _inflation_window_tasks()
    run_id = "refresh-v5-inflation-all-failed"
    _persist_refresh_run(
        run_id=run_id,
        manifest_version="daily-refresh-v5",
        tasks=tasks,
        failed_task=tasks[0],
    )
    _execute(
        "UPDATE refresh_work_items SET status='failed', attempt=2, series_updated=0, "
        "record_count=0, error_code='TimeoutError' WHERE run_id=?",
        (run_id,),
    )

    payload = refresh_status_payload(datetime.now(timezone.utc))
    inflation = next(task for task in payload["tasks"] if task["task"] == "sgs_inflacao")

    assert inflation["status"] == "error"
    assert inflation["series_updated"] == 0
    assert inflation["record_count"] == 0
    assert inflation["error_code"] == "TimeoutError"


def test_refresh_status_fails_closed_when_v5_inflation_window_is_missing(
    isolated_database: None,
) -> None:
    from refresh import refresh_status_payload

    tasks = _inflation_window_tasks()
    _persist_refresh_run(
        run_id="refresh-v5-inflation-missing-window",
        manifest_version="daily-refresh-v5",
        tasks=tasks[:-1],
    )

    payload = refresh_status_payload(datetime.now(timezone.utc))
    inflation = next(task for task in payload["tasks"] if task["task"] == "sgs_inflacao")

    assert inflation["status"] == "partial"
    assert inflation["series_updated"] == 6
    assert inflation["error_code"] == "IncompleteSGSInflationManifest"


def test_refresh_status_omits_only_network_covered_historical_annual_error(
    isolated_database: None,
) -> None:
    from db.refresh_runs import read_latest_run
    from db.store import (
        list_refresh_tasks,
        record_refresh_task_finish,
        record_refresh_task_start,
    )
    from main import app

    old_task = "sgs_juros_w226_2026-01-01_2026-10-25"
    newer_task = "sgs_juros_w226_2026-01-01_2026-10-26"
    failed_at = "2026-10-25T20:04:20+00:00"
    record_refresh_task_start(old_task, "bcb_sgs", 1, failed_at)
    record_refresh_task_finish(
        old_task, "error", 0, 0, 1505, "InvalidSGSPayload", "2026-10-25T20:04:21+00:00"
    )
    run_id = "refresh-v4-covered-tr-window"
    _persist_refresh_run(
        run_id=run_id,
        manifest_version="daily-refresh-v4",
        tasks=_interest_window_tasks("2026-10-26"),
        started_at=datetime(2026, 10, 26, 21, tzinfo=timezone.utc),
    )
    _record_sgs_window_extraction(
        run_id=run_id,
        task_key=newer_task,
        code=226,
        start_date="2026-01-01",
        end_date="2026-10-26",
    )

    with TestClient(app) as client:
        run_before = read_latest_run()
        response = client.get("/api/refresh-status")
        run_after = read_latest_run()

    task_names = [task["task"] for task in response.json()["tasks"]]
    historical_row = next(row for row in list_refresh_tasks() if row["task"] == old_task)
    assert response.status_code == 200
    assert "sgs_juros" in task_names
    assert old_task not in task_names
    aggregate = next(row for row in response.json()["tasks"] if row["task"] == "sgs_juros")
    assert aggregate["series_expected"] == 4
    assert aggregate["series_updated"] == 4
    assert aggregate["status"] == "ok"
    assert run_before == run_after
    assert historical_row["status"] == "error"
    assert historical_row["error"] == "InvalidSGSPayload"


@pytest.mark.parametrize(
    ("scopes", "expected_covered"),
    [
        (
            [
                ("juros", 226, "2017-01-01", "2017-12-31"),
                ("juros", 226, "2015-01-01", "2015-12-31"),
                ("juros", 226, "2016-01-01", "2016-12-31"),
                ("juros", 226, "2016-06-01", "2017-06-30"),
                ("juros", 226, "2016-01-01", "2016-12-31"),
            ],
            True,
        ),
        ([("juros", 226, "2014-01-01", "2017-08-30")], True),
        (
            [
                ("juros", 226, "2015-01-01", "2016-06-30"),
                ("juros", 226, "2016-07-02", "2017-12-31"),
            ],
            False,
        ),
        ([("juros", 226, "2015-06-02", "2017-12-31")], False),
        ([("juros", 226, "2015-01-01", "2017-08-29")], False),
    ],
    ids=["multi-year-merge-overlap-duplicates", "single-earlier-broader", "interior-gap", "prefix-gap", "suffix-gap"],
)
def test_status_projects_legacy_error_only_when_completed_scope_union_covers_it(
    isolated_database: None,
    scopes: list[tuple[str, int, str, str]],
    expected_covered: bool,
) -> None:
    from db.refresh_runs import read_latest_run
    from db.store import list_refresh_tasks

    run_id = f"interval-coverage-{uuid4()}"
    projected, stored_error, before, after = _legacy_window_projection(
        scopes=scopes,
        run_id=run_id,
    )

    old_task = "sgs_juros_w226_2015-06-01_2017-08-30"
    assert (old_task not in projected) is expected_covered
    assert stored_error["status"] == "error"
    assert stored_error["error"] == "InvalidSGSPayload"
    assert before == after == read_latest_run()
    assert next(row for row in list_refresh_tasks() if row["task"] == old_task)["status"] == "error"


@pytest.mark.parametrize(
    ("scope", "options", "pending", "expected_covered"),
    [
        (("juros", 432, "2015-01-01", "2017-12-31"), {}, False, False),
        (("inflacao", 226, "2015-01-01", "2017-12-31"), {}, False, False),
        (("juros", 226, "2015-01-01", "2017-12-31"), {"source": "other"}, False, False),
        (("juros", 226, "2015-01-01", "2017-12-31"), {"qualifier_unit": "index"}, False, False),
        (("juros", 226, "2015-01-01", "2017-12-31"), {"identity_kind": "point"}, False, False),
        (("juros", 226, "2015-01-01", "2017-12-31"), {"contract_version": "v0"}, False, False),
        (("juros", 226, "2015-01-01", "2017-12-31"), {"contract_version": "calendar-year-v1"}, False, False),
        (("juros", 226, "2015-01-01", "2017-12-31"), {"omit_qualifiers": ("contract_version",)}, False, False),
        (("juros", 226, "2015-01-01", "2017-12-31"), {"omit_qualifiers": ("identity_kind",)}, False, False),
        (("juros", 226, "2015-01-01", "2017-12-31"), {"omit_qualifiers": ("unit",)}, False, False),
        (("juros", 226, "2015-01-01", "2017-12-31"), {"qualifier_overrides": {"contract_version": None}}, False, False),
        (("juros", 226, "2015-01-01", "2017-12-31"), {"qualifier_overrides": {"contract_version": ""}}, False, False),
        (("juros", 226, "2015-01-01", "2017-12-31"), {"qualifier_overrides": {"identity_kind": None}}, False, False),
        (("juros", 226, "2015-01-01", "2017-12-31"), {"qualifier_overrides": {"identity_kind": ""}}, False, False),
        (("juros", 226, "2015-01-01", "2017-12-31"), {"qualifier_overrides": {"identity_kind": "Full_Period"}}, False, False),
        (("juros", 226, "2015-01-01", "2017-12-31"), {"qualifier_overrides": {"identity_kind": []}}, False, False),
        (("juros", 226, "2015-01-01", "2017-12-31"), {"qualifier_overrides": {"unit": None}}, False, False),
        (("juros", 226, "2015-01-01", "2017-12-31"), {"qualifier_overrides": {"unit": ""}}, False, False),
        (("juros", 226, "2015-01-01", "2017-12-31"), {"qualifier_overrides": {"unit": "pct_annual"}}, False, False),
        (("juros", 226, "2015-01-01", "2017-12-31"), {"qualifier_overrides": {"series_code": 432}}, False, False),
        (("juros", 226, "2015-01-01", "2017-12-31"), {"qualifier_overrides": {"contract_version": 1}}, False, False),
        (("juros", 226, "2015-01-01", "2017-12-31"), {"qualifier_overrides": {"identity_kind": 1}}, False, False),
        (("juros", 226, "2015-01-01", "2017-12-31"), {"qualifier_overrides": {"unit": 1}}, False, False),
        (("juros", 226, "2015-01-01", "2017-12-31"), {"started_at": "2026-10-24T00:00:00+00:00", "finished_at": "2026-10-24T00:00:01+00:00"}, False, False),
        (("juros", 226, "2015-01-01", "2017-12-31"), {"started_at": "2026-10-27T00:00:00", "finished_at": "2026-10-27T00:00:01"}, False, False),
        (("juros", 226, "2015-01-01", "2017-12-31"), {"started_at": "not-a-time", "finished_at": "not-a-time"}, False, False),
        (("juros", 226, "2015-01-01", "2017-12-31"), {"started_at": "2026-10-27T00:00:01+00:00", "finished_at": "2026-10-27T00:00:00+00:00"}, False, False),
        (("juros", 226, "2015-01-01", "2017-12-31"), {"status": "cache_hit", "network_called": 0}, False, False),
        (("juros", 226, "2015-01-01", "2017-12-31"), {"status": "partial"}, False, False),
        (("juros", 226, "2015-01-01", "2017-12-31"), {"status": "network_error"}, False, False),
        (("juros", 226, "2015-01-01", "2017-12-31"), {"status": "running"}, False, False),
        (("juros", 226, "2015-01-01", "2017-12-31"), {"omit_finish": True}, False, False),
        (("juros", 226, "2015-01-01", "2017-12-31"), {"received_count": None}, False, False),
        (("juros", 226, "2015-01-01", "2017-12-31"), {"valid_count": None}, False, False),
        (("juros", 226, "2015-01-01", "2017-12-31"), {"persisted_count": 0}, False, False),
        (("juros", 226, "2015-01-01", "2017-12-31"), {}, True, False),
        (("juros", 226, "2015-01-01", "2017-12-31"), {}, False, True),
    ],
)
def test_status_does_not_use_unqualified_or_incomplete_recovery_scopes(
    isolated_database: None,
    scope: tuple[str, int, str, str],
    options: dict[str, Any],
    pending: bool,
    expected_covered: bool,
) -> None:
    projected, stored_error, before, after = _legacy_window_projection(
        scopes=[scope],
        run_id=f"unqualified-coverage-{uuid4()}",
        extraction_options=options,
        pending=pending,
    )

    old_task = "sgs_juros_w226_2015-06-01_2017-08-30"
    assert (old_task not in projected) is expected_covered
    assert stored_error["status"] == "error"
    assert stored_error["error"] == "InvalidSGSPayload"
    if before is not None:
        assert after == before


def test_status_retains_legacy_error_when_attempt_timestamp_has_no_timezone(
    isolated_database: None,
) -> None:
    old_task = "sgs_juros_w226_2015-06-01_2017-08-30"
    projected, stored_error, _, _ = _legacy_window_projection(
        scopes=[("juros", 226, "2015-01-01", "2017-12-31")],
        run_id=f"naive-attempt-coverage-{uuid4()}",
        legacy_attempt_at="2026-10-25T20:04:20",
    )

    assert old_task in projected
    assert stored_error["status"] == "error"


@pytest.mark.parametrize(
    ("case", "status", "network_called", "persisted_count", "qualifier_code", "qualifier_start", "old_start", "newer_failed", "superseded"),
    [
        ("cache", "cache_hit", 0, 301, 226, "2026-01-01", "2026-01-01", False, False),
        ("no-request", "success", 0, 301, 226, "2026-01-01", "2026-01-01", False, False),
        ("no-persist", "success", 1, 0, 226, "2026-01-01", "2026-01-01", False, False),
        ("invalid", "validation_error", 1, 301, 226, "2026-01-01", "2026-01-01", False, False),
        ("other-series", "success", 1, 301, 432, "2026-01-01", "2026-01-01", False, False),
        ("broader-prefix", "success", 1, 301, 226, "2026-01-01", "2026-01-02", False, True),
        ("different-query", "success", 1, 301, 226, "2026-01-02", "2026-01-01", False, False),
        ("invalid-query", "success", 1, 301, 226, "not-a-date", "2026-01-01", False, False),
        ("failed-work-item", "success", 1, 301, 226, "2026-01-01", "2026-01-01", True, False),
    ],
)
def test_refresh_status_keeps_annual_error_without_network_validated_persisted_cohort(
    isolated_database: None,
    case: str,
    status: str,
    network_called: int,
    persisted_count: int,
    qualifier_code: int,
    qualifier_start: str,
    old_start: str,
    newer_failed: bool,
    superseded: bool,
) -> None:
    from db.store import record_refresh_task_finish, record_refresh_task_start
    from main import app

    old_task = f"sgs_juros_w226_{old_start}_2026-10-25"
    newer_task = "sgs_juros_w226_2026-01-01_2026-10-26"
    record_refresh_task_start(old_task, "bcb_sgs", 1, "2026-10-25T20:04:20+00:00")
    record_refresh_task_finish(
        old_task, "error", 0, 0, 1505, "InvalidSGSPayload", "2026-10-25T20:04:21+00:00"
    )
    run_id = f"refresh-v4-not-covered-{case}"
    _persist_refresh_run(
        run_id=run_id,
        manifest_version="daily-refresh-v4",
        tasks=_interest_window_tasks("2026-10-26"),
        started_at=datetime(2026, 10, 26, 21, tzinfo=timezone.utc),
        failed_task=newer_task if newer_failed else None,
    )
    _record_sgs_window_extraction(
        run_id=run_id,
        task_key=newer_task,
        code=qualifier_code,
        start_date=qualifier_start,
        end_date="2026-10-26",
        status=status,
        network_called=network_called,
        persisted_count=persisted_count,
    )

    with TestClient(app) as client:
        response = client.get("/api/refresh-status")

    task_names = [task["task"] for task in response.json()["tasks"]]
    assert (old_task not in task_names) is superseded


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
