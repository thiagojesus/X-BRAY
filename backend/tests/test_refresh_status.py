from collections.abc import Iterator
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import patch

import pytest
from fastapi.testclient import TestClient


@pytest.fixture
def isolated_database(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    import db.store as store

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
    with patch("refresh_reporting.list_refresh_tasks", return_value=rows):
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
    with patch("refresh_reporting.list_refresh_tasks", return_value=rows):
        payload = refresh_status_payload(datetime(2026, 9, 12, 12, tzinfo=timezone.utc))

    assert payload["overall_status"] == "partial"
    assert [task["freshness"] for task in payload["tasks"]] == ["stale", "never"]
    assert all(task["error_code"] == "TimeoutError" for task in payload["tasks"])
