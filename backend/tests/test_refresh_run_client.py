import json
from datetime import datetime, timedelta
from time import perf_counter

import pytest
from fastapi.testclient import TestClient

from healthcheck import HealthcheckProbeError
from healthcheck_payloads import JsonObject, JsonValue
from refresh_manifest import CURRENT_REFRESH_MANIFEST_VERSION
from refresh_run_client import (
    RefreshRunClientConfig,
    RefreshRunClientError,
    _next_item,
    _post_item,
    _snapshot,
    _start_run,
    run_resumable_refresh,
)
from tests.real_rate_support import isolated_database


class LostResponseError(HealthcheckProbeError):
    pass


class StatusUnavailableError(RuntimeError):
    pass


def _run_snapshot(
    state: str,
    item_status: str,
    manifest_version: str = "daily-refresh-v6",
) -> JsonObject:
    return {
        "run": {
            "run_id": "client-run",
            "cycle_id": "client-run",
            "state": state,
            "force": False,
            "manifest_version": manifest_version,
            "final_result": None,
        },
        "items": [
            {
                "item_key": "source-one",
                "status": item_status,
                "attempt": 1,
                "claim_expires_at": None,
            }
        ],
    }


def test_client_accepts_v6_on_a_new_workflow() -> None:
    config = RefreshRunClientConfig("https://api.example.com", "client-run", "client-run", "token")

    snapshot = _snapshot(config, _run_snapshot("running", "pending"))

    assert snapshot.run.manifest_version == CURRENT_REFRESH_MANIFEST_VERSION


@pytest.mark.parametrize("category", ["invalid_schema", None, "unknown-v99"])
def test_client_does_not_requeue_terminal_or_unclassified_v6_failure(category: str | None) -> None:
    raw = _run_snapshot("running", "failed")
    raw["items"][0].update({
        "error_category": category, "last_error_category": category,
        "acquisition_attempts": 1,
    })
    config = RefreshRunClientConfig("https://api.example.com", "client-run", "client-run", "token")
    assert _next_item(_snapshot(config, raw)) is None


def test_client_finalizes_deferred_work_beyond_total_wait_budget_without_sleep(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    due = (datetime.now().astimezone() + timedelta(hours=1)).isoformat()
    running = _run_snapshot("running", "pending")
    running["run"]["next_due_at"] = due
    running["items"][0].update({
        "next_attempt_at": due,
        "deferral_reason": "retry_after",
        "error_code": "Deferred",
    })
    terminal = _run_snapshot("partial", "pending")
    terminal["run"]["final_result"] = json.dumps({
        "status": "partial",
        "tasks": [{
            "name": "source-one", "status": "partial", "error_code": "Deferred",
            "acquisition_status": "deferred", "retry_not_before": due,
            "deferral_reason": "retry_after", "error_category": "rate_limited",
        }],
    })
    calls: list[str] = []

    def request(url: str, method: str, _timeout: float, _headers=None, _body=None):
        if method == "POST" and url.endswith("/api/refresh-runs"):
            return 200, running, 0.0
        if method == "POST" and url.endswith("/finalize"):
            calls.append("finalize")
            return 200, terminal, 0.0
        pytest.fail(f"unexpected request: {method} {url}")

    sleep = pytest.MonkeyPatch()
    sleep.setattr("refresh_run_client.time.sleep", lambda _seconds: pytest.fail("wait must be bounded"))
    try:
        result = run_resumable_refresh(
            RefreshRunClientConfig(
                "https://api.example.com", "client-run", "client-run", "token",
                max_in_run_wait_seconds=30,
            ),
            request,
        )
    finally:
        sleep.undo()

    assert calls == ["finalize"]
    assert result.status == "partial"
    assert result.tasks[0]["acquisition_status"] == "deferred"
    assert result.tasks[0]["error_category"] == "rate_limited"


def test_client_waits_once_for_due_item_then_advances(monkeypatch: pytest.MonkeyPatch) -> None:
    due = (datetime.now().astimezone() + timedelta(seconds=0.05)).isoformat()
    deferred = _run_snapshot("running", "pending")
    deferred["run"]["next_due_at"] = due
    deferred["items"][0].update({"next_attempt_at": due, "deferral_reason": "publisher_cooldown"})
    eligible = _run_snapshot("running", "pending")
    terminal = _run_snapshot("succeeded", "succeeded")
    terminal["run"]["final_result"] = json.dumps({
        "status": "ok", "tasks": [{"name": "source-one", "status": "ok", "error_code": None}],
    })
    events: list[str] = []

    def request(url: str, method: str, _timeout: float, _headers=None, _body=None):
        if method == "POST" and url.endswith("/api/refresh-runs"):
            events.append("start")
            return 200, deferred, 0.0
        if method == "GET":
            events.append("status")
            return 200, eligible, 0.0
        if method == "POST" and url.endswith("/advance"):
            events.append("advance")
            return 200, terminal, 0.0
        pytest.fail(f"unexpected request: {method} {url}")

    waits: list[float] = []
    monkeypatch.setattr("refresh_run_client.time.sleep", waits.append)
    result = run_resumable_refresh(
        RefreshRunClientConfig("https://api.example.com", "client-run", "client-run", "token"),
        request,
    )

    assert events == ["start", "status", "advance"]
    assert len(waits) == 1 and 0 < waits[0] <= 0.05
    assert result.status == "ok"


@pytest.mark.parametrize(
    "manifest_version",
    ("daily-refresh-v1", "daily-refresh-v2", "daily-refresh-v3", "daily-refresh-v4", "daily-refresh-v5"),
)
def test_client_accepts_legacy_manifest_only_for_explicit_resume(manifest_version: str) -> None:
    config = RefreshRunClientConfig(
        "https://api.example.com", "client-run", "client-run", "token", resume_existing=True
    )

    snapshot = _snapshot(config, _run_snapshot("running", "pending", manifest_version))

    assert snapshot.run.manifest_version == manifest_version


@pytest.mark.parametrize(
    "manifest_version",
    ("daily-refresh-v1", "daily-refresh-v2", "daily-refresh-v3", "daily-refresh-v4"),
)
def test_client_rejects_legacy_or_unknown_manifest_on_new_workflow(manifest_version: str) -> None:
    config = RefreshRunClientConfig("https://api.example.com", "client-run", "client-run", "token")

    with pytest.raises(RefreshRunClientError, match="stable workflow identity"):
        _snapshot(config, _run_snapshot("running", "pending", manifest_version))


def test_client_rejects_unknown_manifest_even_on_explicit_resume() -> None:
    config = RefreshRunClientConfig(
        "https://api.example.com", "client-run", "client-run", "token", resume_existing=True
    )

    with pytest.raises(RefreshRunClientError, match="stable workflow identity"):
        _snapshot(config, _run_snapshot("running", "pending", "daily-refresh-v7"))


def test_resume_rejects_unsupported_version_despite_untrusted_config_allowlist() -> None:
    config = RefreshRunClientConfig(
        "https://api.example.com",
        "client-run",
        "client-run",
        "token",
        manifest_version="daily-refresh-v7",
        compatible_manifest_versions=("daily-refresh-v7",),
        resume_existing=True,
    )

    with pytest.raises(RefreshRunClientError, match="stable workflow identity"):
        _snapshot(config, _run_snapshot("running", "pending", "daily-refresh-v7"))


def test_client_does_not_allow_legacy_override_for_new_workflow() -> None:
    config = RefreshRunClientConfig(
        "https://api.example.com",
        "client-run",
        "client-run",
        "token",
        manifest_version="daily-refresh-v3",
    )

    with pytest.raises(RefreshRunClientError, match="stable workflow identity"):
        _snapshot(config, _run_snapshot("running", "pending", "daily-refresh-v3"))


def test_client_rejects_malformed_snapshot_payload() -> None:
    config = RefreshRunClientConfig("https://api.example.com", "client-run", "client-run", "token")

    with pytest.raises(RefreshRunClientError, match="invalid status document"):
        _snapshot(config, {})


@pytest.mark.parametrize(
    "identity_field",
    ("run_id", "cycle_id", "force"),
)
def test_client_rejects_v4_snapshot_with_changed_stable_identity(identity_field: str) -> None:
    config = RefreshRunClientConfig("https://api.example.com", "client-run", "client-run", "token")
    payload = _run_snapshot("running", "pending")
    if identity_field == "run_id":
        payload["run"]["run_id"] = "other-run"
    elif identity_field == "cycle_id":
        payload["run"]["cycle_id"] = "other-cycle"
    else:
        payload["run"]["force"] = True

    with pytest.raises(RefreshRunClientError, match="stable workflow identity"):
        _snapshot(config, payload)


@pytest.mark.usefixtures(isolated_database.__name__)
def test_client_starts_and_advances_real_backend_v4_manifest_once(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import refresh_run_execution
    from main import app
    from refresh_reporting import RefreshTaskExecution

    monkeypatch.setattr(
        refresh_run_execution,
        "execute_refresh_item",
        lambda item_task, _snapshot, _dependencies, _now: RefreshTaskExecution(
            {"name": item_task, "status": "ok", "error_code": None},
            0,
            0,
            0,
        ),
    )
    run_id = "client-backend-contract"
    config = RefreshRunClientConfig("http://testserver", run_id, run_id, "test-token")
    calls: list[tuple[str, str]] = []
    task_orders: list[int] = []
    created_at: str | None = None

    with TestClient(app) as client:
        def request_json(
            url: str,
            method: str,
            _timeout: float,
            headers: dict[str, str] | None = None,
            body: JsonValue | None = None,
        ) -> tuple[int, JsonValue, float]:
            nonlocal created_at
            path = url.removeprefix(config.base_url)
            calls.append((method, path))
            started_at = perf_counter()
            response = client.request(method, path, headers=headers, json=body)
            payload = response.json()
            if method == "POST" and path == "/api/refresh-runs" and isinstance(payload, dict):
                items = payload.get("items")
                if isinstance(items, list):
                    task_orders.extend(
                        item["task_order"]
                        for item in items
                        if isinstance(item, dict) and isinstance(item.get("task_order"), int)
                    )
                run = payload.get("run")
                if isinstance(run, dict) and isinstance(run.get("created_at"), str):
                    created_at = run["created_at"]
            return response.status_code, payload, perf_counter() - started_at

        started = _start_run(config, request_json)
        actual_keys = tuple(item.item_key for item in started.items)
        planned_sgs = tuple(key for key in actual_keys if key.startswith("sgs_plan_"))
        first_advanced = _post_item(config, request_json, actual_keys[0])
        persisted = client.get(f"/api/refresh-runs/{run_id}").json()

    assert started.run.manifest_version == CURRENT_REFRESH_MANIFEST_VERSION
    assert started.run.run_id == started.run.cycle_id == run_id
    assert len(actual_keys) == len(set(actual_keys))
    assert task_orders == list(range(len(actual_keys)))
    from db.sgs_refresh_plan import active_sgs_series_codes

    expected_recent_units = len(active_sgs_series_codes())
    assert len(planned_sgs) == expected_recent_units + 4
    assert all(key.startswith("sgs_plan_") for key in actual_keys[:expected_recent_units])
    assert not any(key.startswith("sgs_") and not key.startswith("sgs_plan_") for key in actual_keys)
    assert created_at is not None
    assert first_advanced is not None
    assert first_advanced.run.manifest_version == CURRENT_REFRESH_MANIFEST_VERSION
    assert first_advanced.items[0].status == "succeeded"
    assert sum(item.status == "succeeded" for item in first_advanced.items) == 1
    assert sum(item.status == "pending" for item in first_advanced.items) == len(actual_keys) - 1
    assert persisted["run"]["manifest_version"] == CURRENT_REFRESH_MANIFEST_VERSION
    assert [(method, path.rsplit("/", 1)[-1]) for method, path in calls] == [
        ("POST", "refresh-runs"),
        ("POST", "advance"),
    ]


def test_lost_advance_response_is_reconciled_before_any_replay(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # Given
    current = _run_snapshot("running", "pending")
    calls: list[tuple[str, str, JsonValue | None]] = []
    terminal: JsonObject = {
        "run": {
            "run_id": "client-run",
            "cycle_id": "client-run",
            "state": "succeeded",
            "force": False,
                "manifest_version": "daily-refresh-v6",
            "final_result": json.dumps({
                "status": "ok",
                "tasks": [{"name": "source-one", "status": "ok", "error_code": None}],
            }),
        },
        "items": [
            {
                "item_key": "source-one",
                "status": "succeeded",
                "attempt": 1,
                "claim_expires_at": None,
            }
        ],
    }

    def request(
        url: str,
        method: str,
        _timeout: float,
        _headers: dict[str, str] | None = None,
        body: JsonValue | None = None,
    ) -> tuple[int, JsonValue, float]:
        calls.append((url, method, body))
        if method == "POST" and url.endswith("/api/refresh-runs"):
            return 200, current, 0.1
        if method == "POST" and url.endswith("/advance"):
            current["items"] = [
                {"item_key": "source-one", "status": "succeeded", "attempt": 1, "claim_expires_at": None}
            ]
            raise LostResponseError("response lost after commit")
        if method == "GET":
            return 200, current, 0.1
        if method == "POST" and url.endswith("/finalize"):
            return 200, terminal, 0.1
        pytest.fail(f"unexpected request: {method} {url}")

    monkeypatch.setattr("refresh_run_client.time.sleep", lambda _seconds: None)

    # When
    result = run_resumable_refresh(
        RefreshRunClientConfig("https://api.example.com", "client-run", "client-run", "token"),
        request,
    )

    # Then
    advances = [call for call in calls if call[1] == "POST" and call[0].endswith("/advance")]
    assert len(advances) == 1
    assert advances[0][2] == {"expected_item_key": "source-one"}
    assert result.status == "ok"
    assert result.tasks == [{"name": "source-one", "status": "ok", "error_code": None}]


def test_unavailable_status_after_lost_advance_does_not_retry_mutation(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # Given
    mutation_count = 0

    def request(
        url: str,
        method: str,
        _timeout: float,
        _headers: dict[str, str] | None = None,
        _body: JsonValue | None = None,
    ) -> tuple[int, JsonValue, float]:
        nonlocal mutation_count
        if method == "POST" and url.endswith("/api/refresh-runs"):
            return 200, _run_snapshot("running", "pending"), 0.1
        if method == "POST" and url.endswith("/advance"):
            mutation_count += 1
            raise LostResponseError("response lost")
        raise StatusUnavailableError("status unavailable")

    monkeypatch.setattr("refresh_run_client.time.sleep", lambda _seconds: None)

    # When
    with pytest.raises(RefreshRunClientError, match="could not reconcile"):
        run_resumable_refresh(
            RefreshRunClientConfig("https://api.example.com", "client-run", "client-run", "token"),
            request,
        )

    # Then
    assert mutation_count == 1


def test_resume_existing_checks_persisted_run_before_idempotent_start() -> None:
    terminal = _run_snapshot("succeeded", "succeeded", "daily-refresh-v3")
    terminal["run"]["final_result"] = json.dumps(
        {"status": "ok", "tasks": [{"name": "source-one", "status": "ok", "error_code": None}]}
    )
    calls: list[tuple[str, str]] = []

    def request(
        url: str,
        method: str,
        _timeout: float,
        _headers: dict[str, str] | None = None,
        _body: JsonValue | None = None,
    ) -> tuple[int, JsonValue, float]:
        calls.append((method, url))
        if method == "GET":
            return 200, terminal, 0.1
        if method == "POST" and url.endswith("/api/refresh-runs"):
            return 200, terminal, 0.1
        pytest.fail(f"unexpected request: {method} {url}")

    result = run_resumable_refresh(
        RefreshRunClientConfig(
            "https://api.example.com",
            "client-run",
            "client-run",
            "token",
            resume_existing=True,
        ),
        request,
    )

    assert [method for method, _url in calls] == ["GET", "POST"]
    assert result.status == "ok"


def test_resume_existing_fails_closed_when_run_is_missing() -> None:
    calls = 0

    def request(
        _url: str,
        method: str,
        _timeout: float,
        _headers: dict[str, str] | None = None,
        _body: JsonValue | None = None,
    ) -> tuple[int, JsonValue, float]:
        nonlocal calls
        calls += 1
        assert method == "GET"
        return 404, {}, 0.1

    with pytest.raises(RefreshRunClientError, match="refusing to create a new run"):
        run_resumable_refresh(
            RefreshRunClientConfig(
                "https://api.example.com",
                "missing-run",
                "missing-run",
                "token",
                resume_existing=True,
            ),
            request,
        )

    assert calls == 1


def test_lost_start_response_reuses_the_same_run_id_without_second_start(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # Given
    current = _run_snapshot("running", "pending")
    start_count = 0
    terminal: JsonObject = {
        "run": {
            "run_id": "client-run",
            "cycle_id": "client-run",
            "state": "succeeded",
            "force": False,
                "manifest_version": "daily-refresh-v6",
            "final_result": json.dumps({
                "status": "ok",
                "tasks": [{"name": "source-one", "status": "ok", "error_code": None}],
            }),
        },
        "items": [
            {
                "item_key": "source-one",
                "status": "succeeded",
                "attempt": 1,
                "claim_expires_at": None,
            }
        ],
    }

    def request(
        url: str,
        method: str,
        _timeout: float,
        _headers: dict[str, str] | None = None,
        _body: JsonValue | None = None,
    ) -> tuple[int, JsonValue, float]:
        nonlocal start_count
        if method == "POST" and url.endswith("/api/refresh-runs"):
            start_count += 1
            raise LostResponseError("start response lost after creation")
        if method == "GET":
            return 200, current, 0.1
        if method == "POST" and url.endswith("/advance"):
            current["items"] = [
                {"item_key": "source-one", "status": "succeeded", "attempt": 1, "claim_expires_at": None}
            ]
            return 200, current, 0.1
        if method == "POST" and url.endswith("/finalize"):
            return 200, terminal, 0.1
        pytest.fail(f"unexpected request: {method} {url}")

    monkeypatch.setattr("refresh_run_client.time.sleep", lambda _seconds: None)

    # When
    result = run_resumable_refresh(
        RefreshRunClientConfig("https://api.example.com", "client-run", "client-run", "token"),
        request,
    )

    # Then
    assert start_count == 1
    assert result.status == "ok"
