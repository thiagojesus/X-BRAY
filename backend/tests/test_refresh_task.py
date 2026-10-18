import pytest

import refresh
from db import store
from refresh import RefreshPayload, RefreshTask
from refresh_reporting import TaskStatus
from tests.real_rate_support import isolated_database

pytestmark = pytest.mark.usefixtures(isolated_database.__name__)


@pytest.mark.parametrize(
    ("payload", "expected"),
    [
        ({"first": [1, 2], "second": {"value": 3}}, ("ok", 2, 3)),
        ({"first": [1, 2], "second": []}, ("partial", 1, 2)),
        ({"rows": 4}, ("ok", 1, 4)),
    ],
)
def test_run_refresh_task_records_counts_when_payload_is_usable(
    payload: RefreshPayload,
    expected: tuple[TaskStatus, int, int],
) -> None:
    # Given
    status, series_updated, record_count = expected
    observed_statuses: list[str] = []

    def source() -> RefreshPayload:
        observed_statuses.append(str(store.list_refresh_tasks()[0]["status"]))
        return payload

    task = RefreshTask("single", "provider", "example.test", 2, source)

    # When
    result = refresh.run_refresh_task(task)

    # Then
    assert result == {"name": "single", "status": status, "error_code": None}
    assert observed_statuses == ["running"]
    [row] = store.list_refresh_tasks()
    assert row["task"] == "single"
    assert row["provider"] == "provider"
    assert row["series_expected"] == 2
    assert row["status"] == status
    assert row["series_updated"] == series_updated
    assert row["record_count"] == record_count
    assert row["last_success"] == row["finished_at"]
    assert row["last_attempt"] == row["started_at"]
    assert row["duration_ms"] >= 0
    assert row["error"] is None


@pytest.mark.parametrize(
    ("payload", "error_code"),
    [
        ({"error": "private provider detail", "data": [1]}, "ProviderReportedError"),
        ({"data": []}, "NoCommittedObservations"),
        ({"malformed": 1}, "NoCommittedObservations"),
        ({"rows": True}, "NoCommittedObservations"),
    ],
)
def test_run_refresh_task_preserves_last_success_when_payload_fails(
    payload: RefreshPayload, error_code: str,
) -> None:
    # Given
    previous_success = "2026-09-01T00:00:00+00:00"
    store.record_refresh_task_start("single", "provider", 1, previous_success)
    store.record_refresh_task_finish("single", "ok", 1, 2, 1, None, previous_success)
    task = RefreshTask("single", "provider", "example.test", 1, lambda: payload)

    # When
    result = refresh.run_refresh_task(task)

    # Then
    assert result == {"name": "single", "status": "error", "error_code": error_code}
    [row] = store.list_refresh_tasks()
    assert row["last_success"] == previous_success
    assert row["status"] == "error"
    assert row["series_updated"] == 0
    assert row["record_count"] == 0
    assert row["error"] == error_code
    assert row["finished_at"] != previous_success
    assert "private provider detail" not in str((result, row))


def test_run_refresh_task_sanitizes_exception_when_provider_raises() -> None:
    # Given
    previous_success = "2026-09-01T00:00:00+00:00"
    store.record_refresh_task_start("single", "provider", 1, previous_success)
    store.record_refresh_task_finish("single", "ok", 1, 2, 1, None, previous_success)

    def source() -> RefreshPayload:
        raise RuntimeError("private exception detail")

    task = RefreshTask("single", "provider", "example.test", 1, source)

    # When
    result = refresh.run_refresh_task(task)

    # Then
    assert result == {"name": "single", "status": "error", "error_code": "RuntimeError"}
    [row] = store.list_refresh_tasks()
    assert row["last_success"] == previous_success
    assert row["status"] == "error"
    assert row["series_updated"] == 0
    assert row["record_count"] == 0
    assert row["error"] == "RuntimeError"
    assert row["finished_at"] != previous_success
    assert "private exception detail" not in str((result, row))
