import json
from dataclasses import replace
from datetime import datetime, timedelta, timezone
from email.utils import format_datetime
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from threading import Thread

import pytest
from fastapi.testclient import TestClient

from refresh import RefreshDependencies
from tests.real_rate_support import isolated_database

pytestmark = pytest.mark.usefixtures(isolated_database.__name__)


def _dependencies() -> RefreshDependencies:
    payload = {"data": [{"value": 1}]}
    return RefreshDependencies(
        fetch_sgs_batch=lambda *_args, **_kwargs: payload,
        fetch_sgs_window=lambda _code, *, start_date, **_kwargs: [{"data": start_date, "valor": "1"}],
        fetch_all_focus=lambda **_kwargs: payload,
        fetch_focus_12m=lambda **_kwargs: payload,
        fetch_anbima_ima=lambda **_kwargs: payload,
        fetch_di_curves=lambda **_kwargs: payload,
        fetch_state_polls=lambda **_kwargs: payload,
        refresh_treasury_data=lambda: payload,
        refresh_sidra_data=lambda: payload,
        refresh_derived_snapshots=lambda _as_of, _cycle_id: 3,
        observe_inflation_target_rules=lambda: 11,
        refresh_macro_score=lambda _as_of, _cycle_id: 1,
    )


def test_run_routes_reconcile_replayed_advance_without_running_next_item(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # Given
    import refresh_run_execution
    from db import store
    from main import app

    calls = 0
    dependencies = _dependencies()
    payload = {"data": [{"value": 1}]}

    def fetch(*_args, **_kwargs):
        nonlocal calls
        calls += 1
        return payload

    dependencies = RefreshDependencies(
        fetch_sgs_batch=fetch,
        fetch_sgs_window=lambda _code, *, start_date, end_date, **_kwargs: (
            fetch() and [{"data": start_date, "valor": "1"}, {"data": end_date, "valor": "1"}]
        ),
        fetch_all_focus=fetch,
        fetch_focus_12m=fetch,
        fetch_anbima_ima=fetch,
        fetch_di_curves=fetch,
        fetch_state_polls=fetch,
        refresh_treasury_data=fetch,
        refresh_sidra_data=fetch,
        refresh_derived_snapshots=dependencies.refresh_derived_snapshots,
        observe_inflation_target_rules=dependencies.observe_inflation_target_rules,
        refresh_macro_score=dependencies.refresh_macro_score,
    )
    monkeypatch.setattr(refresh_run_execution, "_dependencies", lambda _value: dependencies)
    with TestClient(app) as client:
        # When
        started = client.post(
            "/api/refresh-runs",
            json={"run_id": "api-run", "cycle_id": "api-cycle", "force": True},
        )
        before_advance = client.get("/api/refresh-runs/api-run")
        first_item = started.json()["items"][0]["item_key"]
        first = client.post(
            "/api/refresh-runs/api-run/advance",
            json={"expected_item_key": first_item},
        )
        replayed = client.post(
            "/api/refresh-runs/api-run/advance",
            json={"expected_item_key": first_item},
        )
        status = client.get("/api/refresh-runs/api-run")

    # Then
    assert started.status_code == 200
    assert before_advance.status_code == 200
    assert before_advance.json()["run"]["updated_at"] == started.json()["run"]["updated_at"]
    assert before_advance.json()["run"]["lease_expires_at"] == started.json()["run"]["lease_expires_at"]
    assert first.status_code == 200
    assert replayed.status_code == 200
    assert status.status_code == 200
    assert calls == 1
    assert replayed.json()["items"][0]["status"] == "succeeded"
    assert replayed.json()["items"][1]["status"] == "pending"
    assert status.json()["run"]["lease_expires_at"] is not None
    assert store.list_refresh_tasks()[0]["status"] == "ok"


def test_v6_permanent_sgs_payload_failure_persists_and_cannot_be_reclaimed(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import refresh_run_execution
    from datafetchers.bcb_sgs import InvalidSGSPayloadError
    from db import store
    from main import app

    calls = 0
    dependencies = _dependencies()

    def fetch_window(_code: int, *, start_date: str, end_date: str, **_kwargs):
        nonlocal calls
        calls += 1
        if calls == 1:
            raise InvalidSGSPayloadError(188, start_date, end_date, "outside-requested-window", 0)
        return [{"data": start_date, "valor": "1.0"}]

    dependencies = replace(dependencies, fetch_sgs_window=fetch_window)
    monkeypatch.setattr(refresh_run_execution, "_dependencies", lambda _value: dependencies)
    with TestClient(app) as client:
        started = client.post("/api/refresh-runs", json={
            "run_id": "v6-permanent-sgs", "cycle_id": "v6-permanent-sgs-cycle", "force": True,
        })
        first_key = started.json()["items"][0]["item_key"]
        failed = client.post("/api/refresh-runs/v6-permanent-sgs/advance", json={
            "expected_item_key": first_key,
        })
        first = next(item for item in failed.json()["items"] if item["item_key"] == first_key)
        assert first["status"] == "failed"
        assert first["last_error_category"] == "invalid_schema"
        assert first["last_error_code"].startswith("InvalidSGSPayload:")
        assert first["attempt"] == 1
        persisted = store._query(
            "SELECT last_error_category, last_error_code FROM refresh_work_items "
            "WHERE run_id = ? AND item_key = ?", ("v6-permanent-sgs", first_key),
        )[0]
        assert persisted == {
            "last_error_category": "invalid_schema", "last_error_code": first["last_error_code"],
        }
        replay = client.post("/api/refresh-runs/v6-permanent-sgs/advance", json={
            "expected_item_key": first_key,
        })
        assert replay.status_code == 200
        assert calls == 1
        next_key = replay.json()["run"]["due_work_item_key"]
        assert next_key is not None and next_key != first_key
        progressed = client.post("/api/refresh-runs/v6-permanent-sgs/advance", json={
            "expected_item_key": next_key,
        })
        assert progressed.status_code == 200
        assert calls == 2


def test_v6_real_local_http_404_is_terminal_and_does_not_write_sgs_proof(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import provider_http
    import refresh_run_execution
    from datafetchers import bcb_sgs
    from db import store
    from main import app

    class NotFoundHandler(BaseHTTPRequestHandler):
        calls = 0

        def do_GET(self) -> None:
            type(self).calls += 1
            self.send_response(404)
            self.send_header("Content-Length", "0")
            self.end_headers()

        def log_message(self, _format: str, *_args: object) -> None:
            return

    server = ThreadingHTTPServer(("127.0.0.1", 0), NotFoundHandler)
    server.daemon_threads = True
    thread = Thread(target=server.serve_forever, daemon=True)
    thread.start()
    local_url = f"http://127.0.0.1:{server.server_port}/sgs.json"
    monkeypatch.setattr(provider_http, "require_allowed_provider_url", lambda _url: local_url)
    dependencies = replace(_dependencies(), fetch_sgs_window=bcb_sgs.fetch_sgs_window)
    monkeypatch.setattr(refresh_run_execution, "_dependencies", lambda _value: dependencies)
    try:
        with TestClient(app) as client:
            started = client.post("/api/refresh-runs", json={
                "run_id": "v6-real-http-404", "cycle_id": "v6-real-http-404-cycle", "force": True,
            })
            assert started.status_code == 200
            first_key = started.json()["items"][0]["item_key"]
            before_receipts = store._query("SELECT COUNT(*) AS n FROM sgs_coverage_receipts")[0]["n"]
            before_commits = store._query("SELECT COUNT(*) AS n FROM sgs_unit_commits")[0]["n"]
            failed = client.post("/api/refresh-runs/v6-real-http-404/advance", json={
                "expected_item_key": first_key,
            })
            item = next(row for row in failed.json()["items"] if row["item_key"] == first_key)
            replay = client.post("/api/refresh-runs/v6-real-http-404/advance", json={
                "expected_item_key": first_key,
            })
            after_receipts = store._query("SELECT COUNT(*) AS n FROM sgs_coverage_receipts")[0]["n"]
            after_commits = store._query("SELECT COUNT(*) AS n FROM sgs_unit_commits")[0]["n"]
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=1)

    assert failed.status_code == replay.status_code == 200
    assert item["status"] == "failed" and item["attempt"] == 1
    assert item["last_error_category"] == "configuration_error"
    assert "HTTPStatusError" in item["last_error_code"]
    assert "404" in item["last_error_code"]
    assert NotFoundHandler.calls == 1
    assert before_receipts == after_receipts == 0
    assert before_commits == after_commits == 0


def test_provider_admission_race_is_durably_deferred_without_dispatch(monkeypatch: pytest.MonkeyPatch) -> None:
    import provider_http
    import refresh_run_execution
    from datafetchers import bcb_sgs
    from db import store
    from main import app

    base = _dependencies()
    dependencies = RefreshDependencies(
        fetch_sgs_batch=base.fetch_sgs_batch,
        fetch_sgs_window=bcb_sgs.fetch_sgs_window,
        fetch_all_focus=base.fetch_all_focus,
        fetch_focus_12m=base.fetch_focus_12m,
        fetch_anbima_ima=base.fetch_anbima_ima,
        fetch_di_curves=base.fetch_di_curves,
        fetch_state_polls=base.fetch_state_polls,
        refresh_treasury_data=base.refresh_treasury_data,
        refresh_sidra_data=base.refresh_sidra_data,
        refresh_derived_snapshots=base.refresh_derived_snapshots,
        observe_inflation_target_rules=base.observe_inflation_target_rules,
        refresh_macro_score=base.refresh_macro_score,
    )
    monkeypatch.setattr(refresh_run_execution, "_dependencies", lambda _value: dependencies)
    monkeypatch.setattr(bcb_sgs, "get_provider_response_bounded", provider_http.get_provider_response_bounded)
    dispatches = 0

    def fail_dispatch(*_args, **_kwargs):
        nonlocal dispatches
        dispatches += 1
        raise AssertionError("unexpected raw HTTP dispatch")

    monkeypatch.setattr(provider_http.httpx.AsyncClient, "stream", fail_dispatch)
    execute = refresh_run_execution.execute_refresh_item

    def race_after_peek(item_task, snapshot, active, now):
        store._execute(
            """INSERT INTO source_access_state
               (scope_id, consecutive_temporary_failures, state, retry_not_before, updated_at)
               VALUES ('bcb_sgs:primary:primary', 3, 'open', ?, ?)
               ON CONFLICT(scope_id) DO UPDATE SET state='open', retry_not_before=excluded.retry_not_before,
                 probe_lease_until=NULL, opened_until=NULL""",
            ((now + timedelta(hours=1)).isoformat(), now.isoformat()),
        )
        return execute(item_task, snapshot, active, now)

    monkeypatch.setattr(refresh_run_execution, "execute_refresh_item", race_after_peek)
    with TestClient(app) as client:
        started = client.post(
            "/api/refresh-runs", json={"run_id": "race-run", "cycle_id": "race-run", "force": True}
        )
        item = started.json()["items"][0]
        advanced = client.post(
            "/api/refresh-runs/race-run/advance", json={"expected_item_key": item["item_key"]}
        )

    assert started.status_code == advanced.status_code == 200
    deferred = next(entry for entry in advanced.json()["items"] if entry["item_key"] == item["item_key"])
    assert deferred["status"] == "pending"
    assert deferred["deferral_reason"] == "publisher_cooldown"
    assert deferred["next_attempt_at"] is not None
    assert deferred["attempt"] == deferred["claim_generation"] == 1
    assert deferred["acquisition_attempts"] == 0
    assert dispatches == 0


@pytest.mark.parametrize("retry_after_kind", ["seconds", "http_date", "missing", "invalid"])
def test_actual_sgs_429_is_deferred_with_advice_without_second_dispatch(
    retry_after_kind: str, monkeypatch: pytest.MonkeyPatch,
) -> None:
    import provider_http
    import refresh_run_execution
    from datafetchers import bcb_sgs
    from db import store
    from main import app

    now = datetime.now(timezone.utc)
    retry_header = {
        "seconds": "3600",
        "http_date": format_datetime(now + timedelta(hours=1), usegmt=True),
        "missing": None,
        "invalid": "not-a-date",
    }[retry_after_kind]

    class Handler(BaseHTTPRequestHandler):
        calls = 0

        def do_GET(self) -> None:
            type(self).calls += 1
            self.send_response(429)
            if retry_header is not None:
                self.send_header("Retry-After", retry_header)
            self.send_header("Content-Length", "0")
            self.end_headers()

        def log_message(self, _format: str, *_args: object) -> None:
            return

    Handler.calls = 0
    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    server.daemon_threads = True
    thread = Thread(target=server.serve_forever, daemon=True)
    thread.start()
    monkeypatch.setattr(bcb_sgs, "BCB_SGS_BASE", f"http://127.0.0.1:{server.server_port}/series/{{code}}")
    monkeypatch.setattr(provider_http, "require_allowed_provider_url", lambda url: url)
    base = _dependencies()
    dependencies = RefreshDependencies(
        fetch_sgs_batch=base.fetch_sgs_batch,
        fetch_sgs_window=bcb_sgs.fetch_sgs_window,
        fetch_all_focus=base.fetch_all_focus,
        fetch_focus_12m=base.fetch_focus_12m,
        fetch_anbima_ima=base.fetch_anbima_ima,
        fetch_di_curves=base.fetch_di_curves,
        fetch_state_polls=base.fetch_state_polls,
        refresh_treasury_data=base.refresh_treasury_data,
        refresh_sidra_data=base.refresh_sidra_data,
        refresh_derived_snapshots=base.refresh_derived_snapshots,
        observe_inflation_target_rules=base.observe_inflation_target_rules,
        refresh_macro_score=base.refresh_macro_score,
    )
    monkeypatch.setattr(refresh_run_execution, "_dependencies", lambda _value: dependencies)
    try:
        with TestClient(app) as client:
            started = client.post(
                "/api/refresh-runs",
                json={"run_id": f"429-{retry_after_kind}", "cycle_id": f"429-{retry_after_kind}", "force": True},
            )
            item = started.json()["items"][0]
            response = client.post(
                f"/api/refresh-runs/429-{retry_after_kind}/advance",
                json={"expected_item_key": item["item_key"]},
            )
            first_row = store._query(
                "SELECT status, attempt, claim_generation, acquisition_attempts, next_attempt_at, "
                "deferral_reason, claim_token FROM refresh_work_items WHERE run_id = ? AND item_key = ?",
                (f"429-{retry_after_kind}", item["item_key"]),
            )[0]
            store._execute(
                "UPDATE refresh_work_items SET next_attempt_at = ? WHERE run_id = ? AND item_key = ?",
                ("2000-01-01T00:00:00+00:00", f"429-{retry_after_kind}", item["item_key"]),
            )
            store._execute(
                "UPDATE source_access_state SET state = 'closed', retry_not_before = NULL, "
                "opened_until = NULL, probe_lease_until = NULL WHERE scope_id = ?",
                ("bcb_sgs:primary:primary",),
            )
            second = client.post(
                f"/api/refresh-runs/429-{retry_after_kind}/advance",
                json={"expected_item_key": item["item_key"]},
            )
            assert second.status_code == 200
            second_row = store._query(
                "SELECT status, attempt, claim_generation, acquisition_attempts, next_attempt_at "
                "FROM refresh_work_items WHERE run_id = ? AND item_key = ?",
                (f"429-{retry_after_kind}", item["item_key"]),
            )[0]
            third = client.post(
                f"/api/refresh-runs/429-{retry_after_kind}/advance",
                json={"expected_item_key": item["item_key"]},
            )
            assert third.status_code == 200
            extraction = store._query(
                "SELECT error_category, request_count, persisted_count FROM source_extractions "
                "WHERE run_id = ? ORDER BY started_at DESC LIMIT 1",
                (f"429-{retry_after_kind}",),
            )[0]
            store._execute(
                "UPDATE refresh_work_items SET status = 'skipped', finished_at = ? "
                "WHERE run_id = ? AND item_key <> ?",
                (datetime.now(timezone.utc).isoformat(), f"429-{retry_after_kind}", item["item_key"]),
            )
            finalized = client.post(f"/api/refresh-runs/429-{retry_after_kind}/finalize")
            final_run = finalized.json()["run"]
            final_tasks = json.loads(final_run["final_result"])["tasks"]
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=1)

    assert started.status_code == response.status_code == 200
    assert first_row["status"] == "pending"
    assert first_row["deferral_reason"] == "retry_after"
    assert first_row["claim_token"] is None
    response_item = next(
        value for value in response.json()["items"] if value["item_key"] == item["item_key"]
    )
    assert response_item["last_error_category"] == "rate_limited"
    assert datetime.fromisoformat(response.json()["run"]["next_due_at"]) >= datetime.fromisoformat(
        first_row["next_attempt_at"]
    )
    assert response.json()["run"]["due_work_item_key"] != item["item_key"]
    assert first_row["attempt"] == first_row["claim_generation"] == 1
    assert first_row["acquisition_attempts"] == 1
    minimum_cooldown = timedelta(minutes=59) if retry_after_kind in {"seconds", "http_date"} else timedelta(minutes=4)
    assert datetime.fromisoformat(first_row["next_attempt_at"]) >= now + minimum_cooldown
    assert extraction["error_category"] == "rate_limited"
    assert extraction["request_count"] == 1
    assert extraction["persisted_count"] in (None, 0)
    assert second_row["status"] == "pending"
    assert second_row["attempt"] == second_row["claim_generation"] == 2
    assert second_row["acquisition_attempts"] == 2
    assert Handler.calls == 2
    assert store._query("SELECT * FROM sgs WHERE series_code = ?", (432,)) == []
    assert store.get_meta("sgs_last_refresh_432") is None
    assert finalized.status_code == 200
    assert final_run["state"] == "partial"
    assert final_run["lease_expires_at"] is None
    deferred_task = next(task for task in final_tasks if task["name"] == item["task"])
    assert deferred_task["acquisition_status"] == "deferred"
    assert deferred_task["error_category"] == "rate_limited"
    assert deferred_task["retry_not_before"] == second_row["next_attempt_at"]
    assert deferred_task["deferral_reason"] == "retry_after"
    assert store.get_meta("last_successful_update") is None


def test_real_client_fastapi_sqlite_finalizes_long_429_cooldown(monkeypatch: pytest.MonkeyPatch) -> None:
    import provider_http
    import refresh_run_client
    import refresh_run_execution
    from datafetchers import bcb_sgs
    from db import store
    from main import app

    retry_header = "3600"

    class Handler(BaseHTTPRequestHandler):
        calls = 0

        def do_GET(self) -> None:
            type(self).calls += 1
            self.send_response(429)
            self.send_header("Retry-After", retry_header)
            self.send_header("Content-Length", "0")
            self.end_headers()

        def log_message(self, _format: str, *_args: object) -> None:
            return

    Handler.calls = 0
    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    server.daemon_threads = True
    thread = Thread(target=server.serve_forever, daemon=True)
    thread.start()
    monkeypatch.setattr(bcb_sgs, "BCB_SGS_BASE", f"http://127.0.0.1:{server.server_port}/series/{{code}}")
    monkeypatch.setattr(provider_http, "require_allowed_provider_url", lambda url: url)
    monkeypatch.setattr(
        refresh_run_execution,
        "_dependencies",
        lambda _value: replace(_dependencies(), fetch_sgs_window=bcb_sgs.fetch_sgs_window),
    )
    run_id = "client-api-long-cooldown"
    retained_keys: set[str] = set()
    first_item_key: str | None = None
    try:
        with TestClient(app) as client:
            def request_json(url, method, timeout, headers=None, body=None):
                nonlocal first_item_key
                del timeout
                path = url.removeprefix("https://api.example.test")
                response = client.request(method, path, headers=headers, json=body)
                if method == "POST" and path == "/api/refresh-runs" and response.status_code == 200:
                    started = response.json()
                    first_item = started["items"][0]
                    first_item_key = first_item["item_key"]
                    focus_item = next(value for value in started["items"] if value["task"] == "focus")
                    retained_keys.update({first_item["item_key"], focus_item["item_key"]})
                if (
                    method == "POST"
                    and path.endswith("/advance")
                    and response.status_code == 200
                    and body["expected_item_key"] == first_item_key
                ):
                    placeholders = ",".join("?" for _ in retained_keys)
                    store._execute(
                        "UPDATE refresh_work_items SET status = 'skipped', finished_at = ? "
                        f"WHERE run_id = ? AND item_key NOT IN ({placeholders})",
                        (datetime.now(timezone.utc).isoformat(), run_id, *retained_keys),
                    )
                    payload = client.get(f"/api/refresh-runs/{run_id}", headers=headers).json()
                    return response.status_code, payload, 0.0
                return response.status_code, response.json(), 0.0

            result = refresh_run_client.run_resumable_refresh(
                refresh_run_client.RefreshRunClientConfig(
                    "https://api.example.test", run_id, run_id, "test-token", max_in_run_wait_seconds=0,
                ),
                request_json,
            )
            stored = store._query(
                "SELECT status, acquisition_attempts, next_attempt_at FROM refresh_work_items "
                "WHERE run_id = ? AND deferral_reason = 'retry_after' LIMIT 1",
                (run_id,),
            )[0]
            lease = store._query("SELECT * FROM refresh_leases WHERE lock_key = 'daily_refresh'")
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=1)

    assert result.status == "partial"
    assert result.tasks[0]["acquisition_status"] == "deferred"
    assert result.tasks[0]["retry_not_before"] == stored["next_attempt_at"]
    assert result.tasks[0]["last_error_code"]
    assert next(task for task in result.tasks if task["name"] == "focus")["status"] == "ok"
    assert stored["status"] == "pending"
    assert stored["acquisition_attempts"] == 1
    assert lease == []
    assert Handler.calls == 1


def test_run_routes_reject_unknown_runs_and_changed_manifests(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # Given
    import refresh_run_execution
    from main import app

    dependencies = _dependencies()

    with TestClient(app) as client:
        # When
        unknown = client.get("/api/refresh-runs/no-such-run")
        started = client.post(
            "/api/refresh-runs",
            json={"run_id": "manifest-run", "cycle_id": "manifest-cycle", "force": True},
        )
        monkeypatch.setattr(refresh_run_execution, "_dependencies", lambda _value: dependencies)
        changed = client.post(
            "/api/refresh-runs",
            json={"run_id": "manifest-run", "cycle_id": "manifest-cycle", "force": False},
        )

    # Then
    assert unknown.status_code == 404
    assert started.status_code == 200
    assert changed.status_code == 409
    assert changed.json() == {"detail": {"error": "refresh_run_manifest_conflict"}}


def test_run_routes_require_refresh_auth_outside_test_mode(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # Given
    from main import app

    monkeypatch.setenv("XBRAY_ENV", "production")
    monkeypatch.delenv("XBRAY_REFRESH_TOKEN", raising=False)

    with TestClient(app, raise_server_exceptions=False) as client:
        # When
        response = client.post(
            "/api/refresh-runs",
            json={"run_id": "auth-run", "cycle_id": "auth-cycle", "force": False},
        )

    # Then
    assert response.status_code == 503
    assert response.json() == {"detail": "refresh_auth_unconfigured"}
