from __future__ import annotations

import base64
import json
from datetime import datetime, timedelta, timezone
from uuid import uuid4

import pytest
from fastapi.testclient import TestClient

import admin_auth
import admin_monitoring as monitoring
from admin_auth_config import AuthConfig
from db import admin_auth as auth_store
from db import store
from main import app


def config() -> AuthConfig:
    return AuthConfig(True, "https://auth.invalid", "synthetic-anon", "http://localhost:5173",
                      "http://localhost:5173/api/auth/callback",
                      "http://localhost:5173/api/auth/callback?flow_state=*",
                      b"f" * 32, "test-v1", b"s" * 32, 1800, 28800, True,
                      ("localhost:5173",))


@pytest.fixture()
def admin_client(monkeypatch: pytest.MonkeyPatch, tmp_path):
    if store._conn is not None:
        store._conn.close()
    store._conn = None
    monkeypatch.setattr(store, "DATABASE_URL", "")
    monkeypatch.setattr(store, "DB_PATH", tmp_path / "admin-read.sqlite")
    store.init_db()
    monkeypatch.setattr(admin_auth, "_config", config)
    user_id = str(uuid4())
    auth_store.grant_admin(user_id, granted_by=str(uuid4()), reason="synthetic test grant")
    raw, _csrf = admin_auth._establish_session(None, config(), user_id)
    with TestClient(app, base_url=config().app_origin) as client:
        client.cookies.set(admin_auth.DEV_SESSION_COOKIE, raw)
        yield client
    store._conn.close()
    store._conn = None


def _insert_run(run_id: str, created_at: str, state: str = "succeeded", final_result: object = None):
    store.execute_statement(
        "INSERT INTO refresh_runs (run_id, cycle_id, state, force, manifest_version, created_at, updated_at, finished_at, final_result) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (run_id, f"cycle-{run_id}", state, 0, "daily-refresh-v6", created_at, created_at, created_at if state != "running" else None,
         json.dumps(final_result) if final_result is not None else None),
    )


def _insert_item(run_id: str, task: str, order: int, status: str = "succeeded", attempt: int = 1):
    created = "2026-01-01T00:00:00Z"
    store.execute_statement(
        "INSERT INTO refresh_work_items (run_id,item_key,task,task_order,status,attempt,created_at,updated_at,finished_at,series_updated,record_count,error_code,claim_generation,acquisition_attempts) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
        (run_id, f"{run_id}-{order}", task, order, status, attempt, created, created, created, 2, 4, "ReadTimeout", 1, 1),
    )


def test_private_routes_require_human_session_and_no_store(admin_client: TestClient, monkeypatch: pytest.MonkeyPatch):
    human_cookie = admin_client.cookies.get(admin_auth.DEV_SESSION_COOKIE)
    admin_client.cookies.clear()
    response = admin_client.get("/api/admin/overview", headers={"X-Refresh-Token": "automation-only"})
    assert response.status_code == 401
    assert response.headers["cache-control"] == "no-store"
    assert response.headers["referrer-policy"] == "no-referrer"
    unauthorized_routes = (
        "/api/admin/overview",
        "/api/admin/runs",
        "/api/admin/runs/not-found",
        "/api/admin/providers",
        "/api/admin/coverage?provider=focus",
    )
    for path in unauthorized_routes:
        denied = admin_client.get(path, headers={"X-Refresh-Token": "automation-only"})
        assert denied.status_code == 401
        assert denied.headers["cache-control"] == "no-store"
        assert denied.headers["referrer-policy"] == "no-referrer"
    admin_client.cookies.set(admin_auth.DEV_SESSION_COOKIE, human_cookie)
    monkeypatch.setenv("XBRAY_ENV", "production")
    monkeypatch.setenv("XBRAY_REFRESH_TOKEN", "synthetic-ops-token")
    operation = admin_client.post("/api/refresh-runs", json={"run_id": "human-cannot-start", "cycle_id": "test-cycle", "force": False})
    assert operation.status_code == 401


def test_revoked_membership_and_auth_configuration_errors_keep_private_headers(admin_client: TestClient, monkeypatch: pytest.MonkeyPatch):
    user_id = store._query("SELECT user_id FROM admin_sessions LIMIT 1")[0]["user_id"]
    store.execute_statement("UPDATE admin_memberships SET revoked_at = ? WHERE user_id = ?", (datetime.now(timezone.utc).isoformat(), user_id))
    denied = admin_client.get("/api/admin/runs/missing")
    assert denied.status_code == 403
    assert denied.headers["cache-control"] == "no-store"
    assert denied.headers["referrer-policy"] == "no-referrer"
    assert "user_id" not in denied.text

    def unavailable_config():
        raise admin_auth.AuthUnavailable

    monkeypatch.setattr(admin_auth, "_config", unavailable_config)
    unavailable = admin_client.get("/api/admin/overview")
    assert unavailable.status_code == 503
    assert unavailable.headers["cache-control"] == "no-store"
    assert unavailable.json()["error"] == "auth_unavailable"


def test_monitoring_poll_does_not_extend_admin_idle_session(admin_client: TestClient):
    raw_cookie = admin_client.cookies.get(admin_auth.DEV_SESSION_COOKIE)
    assert raw_cookie is not None
    token_hash = admin_auth._session_hash(raw_cookie, config())
    before = auth_store.get_session(token_hash)
    assert before is not None
    response = admin_client.get("/api/admin/overview")
    after = auth_store.get_session(token_hash)
    assert response.status_code == 200
    assert after is not None
    assert after["idle_expires_at"] == before["idle_expires_at"]


def test_overview_empty_state_and_separates_latest_attempt_from_success(admin_client: TestClient):
    empty = admin_client.get("/api/admin/overview")
    assert empty.status_code == 200
    assert empty.json()["state"] == "no_backend_run_record"
    assert empty.json()["latest_attempt"] is None
    assert empty.json()["latest_fully_successful"] is None
    timestamp = datetime.now(timezone.utc).isoformat()
    _insert_run("old-success", timestamp, final_result={"status": "ok", "tasks": []})
    _insert_run("latest-partial", (datetime.now(timezone.utc) + timedelta(seconds=1)).isoformat(), "partial")
    _insert_item("latest-partial", "sgs_123", 0, "failed")
    response = admin_client.get("/api/admin/overview")
    body = response.json()
    assert body["latest_attempt"]["run_id"] == "latest-partial"
    assert body["latest_fully_successful"]["run_id"] == "old-success"
    assert body["latest_attempt"]["task_counts"]["sgs_123"]["failed"] == 1
    assert response.headers["cache-control"] == "no-store"


def test_run_history_keyset_cursor_is_bound_to_filters_and_anchor(admin_client: TestClient):
    base = datetime(2026, 1, 1, tzinfo=timezone.utc)
    for index in range(4):
        stored = (base + timedelta(days=index)).isoformat()
        if index == 3:
            stored = (base + timedelta(days=index)).astimezone(timezone(timedelta(hours=-3))).isoformat()
        _insert_run(f"run-{index}", stored)
    query = "?limit=2&since=2025-12-01T00:00:00Z&until=2027-02-01T00:00:00Z"
    first = admin_client.get(f"/api/admin/runs{query}")
    assert first.status_code == 200
    first_body = first.json()
    assert [item["run_id"] for item in first_body["items"]] == ["run-3", "run-2"]
    assert first_body["has_more"] is True
    _insert_run("inserted-later", (datetime.now(timezone.utc) + timedelta(seconds=10)).isoformat())
    second = admin_client.get("/api/admin/runs", params={
        "limit": 2, "since": "2025-12-01T00:00:00Z", "until": "2027-02-01T00:00:00Z",
        "cursor": first_body["next_cursor"],
    })
    assert second.status_code == 200, second.text
    assert [item["run_id"] for item in second.json()["items"]] == ["run-1", "run-0"]
    assert len({item["run_id"] for item in first_body["items"] + second.json()["items"]}) == 4
    mismatch = admin_client.get("/api/admin/runs?limit=3&since=2025-12-01T00:00:00Z&until=2027-02-01T00:00:00Z", params={"cursor": first_body["next_cursor"]})
    assert mismatch.status_code == 422
    assert mismatch.headers["cache-control"] == "no-store"


def test_run_history_validates_timezone_period_state_and_limit(admin_client: TestClient):
    for query in (
        "?limit=101", "?state=unknown", "?since=2026-01-01", "?since=2026-02-01T00:00:00Z&until=2026-01-01T00:00:00Z",
    ):
        response = admin_client.get(f"/api/admin/runs{query}")
        assert response.status_code == 422
        assert response.headers["cache-control"] == "no-store"


def test_run_history_default_period_cursor_reuses_first_page_anchor(admin_client: TestClient):
    now = datetime.now(timezone.utc)
    _insert_run("recent-a", (now - timedelta(hours=2)).isoformat())
    _insert_run("recent-b", (now - timedelta(hours=3)).isoformat())
    first = admin_client.get("/api/admin/runs?limit=1")
    assert first.status_code == 200
    _insert_run("new-after-anchor", (datetime.now(timezone.utc) + timedelta(seconds=5)).isoformat())
    second = admin_client.get("/api/admin/runs", params={"limit": 1, "cursor": first.json()["next_cursor"]})
    assert second.status_code == 200
    assert second.json()["items"][0]["run_id"] == "recent-b"
    assert "new-after-anchor" not in {item["run_id"] for item in first.json()["items"] + second.json()["items"]}


def test_equivalent_timezone_run_timestamps_use_stable_run_id_tiebreaker(admin_client: TestClient):
    _insert_run("same-a", "2026-02-01T00:00:00Z")
    _insert_run("same-b", "2026-01-31T21:00:00-03:00")
    response = admin_client.get("/api/admin/runs?since=2026-01-01T00:00:00Z&until=2026-03-01T00:00:00Z")
    assert response.status_code == 200
    ids = [item["run_id"] for item in response.json()["items"]]
    assert ids.index("same-b") < ids.index("same-a")


def test_detail_whitelists_task_and_final_json_and_diagnostics_are_run_bound(admin_client: TestClient):
    run_id = "safe-run"
    _insert_run(run_id, "2026-01-01T00:00:00+00:00", final_result={"status": "partial", "tasks": [{"name": "x", "status": "partial", "error_code": "SECRET_CANARY"}], "secret": "SECRET_CANARY"})
    _insert_item(run_id, "sgs_123", 0, "failed")
    response = admin_client.get(f"/api/admin/runs/{run_id}?task_limit=1")
    assert response.status_code == 200
    body = response.json()
    encoded = response.text
    assert "claim_token" not in encoded and "SECRET_CANARY" not in encoded and "final_result" not in encoded
    assert body["run"]["recorded_final_summary"] == {"status": "partial", "task_count": 1}
    assert body["task_page"]["items"][0]["attempt_count"] == 1
    assert body["task_page"]["items"][0]["error_code"] == "ReadTimeout"
    assert body["current_freshness"]["status"] == "not_included"
    assert body["diagnostics"]["run_id"] == run_id


def test_detail_authorizes_before_returning_unknown_run_and_bounds_task_page(admin_client: TestClient):
    missing = admin_client.get("/api/admin/runs/does-not-exist")
    assert missing.status_code == 404
    assert missing.headers["cache-control"] == "no-store"
    run_id = "task-page"
    _insert_run(run_id, "2026-01-01T00:00:00Z")
    for index in range(3):
        _insert_item(run_id, f"task_{index}", index)
    page = admin_client.get(f"/api/admin/runs/{run_id}?task_limit=2")
    assert page.json()["task_page"]["total"] == 3
    assert page.json()["task_page"]["has_more"] is True
    next_page = admin_client.get(f"/api/admin/runs/{run_id}", params={"task_limit": 2, "task_cursor": page.json()["task_page"]["next_cursor"]})
    assert len(next_page.json()["task_page"]["items"]) == 1


def test_provider_and_coverage_views_are_curated_and_read_only(admin_client: TestClient):
    store.execute_statement("INSERT INTO source_access_state (scope_id, state, opened_until, updated_at) VALUES (?, ?, ?, ?)",
                            ("bcb_sgs:primary:primary", "open", "2000-01-01T00:00:00Z", "2026-01-01T00:00:00Z"))
    before = store._query("SELECT state, opened_until FROM source_access_state")
    providers = admin_client.get("/api/admin/providers")
    assert providers.status_code == 200
    sgs = next(item for item in providers.json()["items"] if item["provider"] == "bcb_sgs")
    assert sgs["circuit"]["recorded_state"] == "open"
    assert sgs["circuit"]["admission_view"] == "next_admission_reassessment_due"
    assert "claim_token" not in providers.text and "probe" not in providers.text.lower() or "probe_lease_until" in providers.text
    coverage = admin_client.get("/api/admin/coverage?provider=bcb_sgs&limit=2")
    assert coverage.status_code == 200
    assert coverage.json()["has_more"] is True
    assert len(coverage.json()["items"]) == 2
    assert coverage.json()["items"][0]["evidence_basis"].startswith("exact_plan_bound")
    unsupported = admin_client.get("/api/admin/coverage?provider=focus")
    assert unsupported.status_code == 200
    assert unsupported.json()["coverage_status"] == "metadata_unknown"
    assert store._query("SELECT state, opened_until FROM source_access_state") == before


def test_coverage_pagination_and_empty_unknown_proof_are_explicit(admin_client: TestClient):
    first = admin_client.get("/api/admin/coverage?provider=bcb_sgs&limit=1")
    assert first.status_code == 200
    body = first.json()
    assert body["items"][0]["status_flags"] == ["acquisition_unverified", "history_pending"]
    second = admin_client.get("/api/admin/coverage", params={"provider": "bcb_sgs", "limit": 1, "cursor": body["next_cursor"]})
    assert second.status_code == 200
    assert second.json()["items"][0]["series_code"] != body["items"][0]["series_code"]
    assert admin_client.get("/api/admin/coverage?provider=bcb_sgs&cursor=999999").status_code == 422


def test_every_endpoint_is_cache_private_and_never_refreshes_on_get(admin_client: TestClient):
    endpoints = ["/api/admin/overview", "/api/admin/runs", "/api/admin/providers", "/api/admin/coverage?provider=bcb_sgs"]
    for endpoint in endpoints:
        before = store._query("SELECT COUNT(*) AS n FROM sgs_unit_commits")[0]["n"]
        response = admin_client.get(endpoint)
        after = store._query("SELECT COUNT(*) AS n FROM sgs_unit_commits")[0]["n"]
        assert response.status_code == 200
        assert response.headers["cache-control"] == "no-store"
        assert response.headers["referrer-policy"] == "no-referrer"
        assert before == after


def test_each_non_sgs_provider_returns_explicit_unknown_without_sgs_coverage_reads(
    admin_client: TestClient,
    monkeypatch: pytest.MonkeyPatch,
):
    provider_names = [provider.value for provider in monitoring.ProviderCode if provider is not monitoring.ProviderCode.BCB_SGS]
    calls: list[tuple[str, tuple[object, ...]]] = []

    def read(sql: str, params: tuple[object, ...] = ()):
        calls.append((sql, params))
        assert "sgs_coverage_receipts" not in sql
        assert "sgs_unit_commits" not in sql
        return [{"captured_at": None}]

    def forbidden_sgs_reads(*args, **kwargs):
        raise AssertionError("non-SGS coverage must not use the SGS catalog or receipt projection")

    monkeypatch.setattr(monitoring, "_read", read)
    monkeypatch.setattr(monitoring, "_coverage_reads", forbidden_sgs_reads)
    for provider in provider_names:
        response = admin_client.get("/api/admin/coverage", params={"provider": provider})
        assert response.status_code == 200
        assert response.headers["cache-control"] == "no-store"
        body = response.json()
        assert body["provider"] == provider
        assert body["coverage_status"] == "metadata_unknown"
        assert body["item_count"] is None and body["item_count_status"] == "metadata_unknown"
        assert body["items"] == [] and body["items_status"] == "not_recorded"
        assert body["latest_valid_capture_at"] is None and body["capture_status"] == "not_recorded"
        assert body["reason"] == "provider_specific_series_coverage_contract_not_available"
    assert len(calls) == 6  # five capture reads plus the native tesouro reference receipt; no SGS proof query
    invalid = admin_client.get("/api/admin/coverage?provider=unknown_provider")
    assert invalid.status_code == 422
    assert invalid.headers["cache-control"] == "no-store"


def test_non_sgs_coverage_preserves_only_native_treasury_receipt_facts(admin_client: TestClient):
    extraction = str(uuid4())
    timestamp = "2026-10-28T12:00:00Z"
    store.execute_statement(
        "INSERT INTO source_extractions (extraction_id,source,indicator,status,network_called,started_at,finished_at,run_id,task_key,network_captured_at,network_reference_count) VALUES (?,?,?,?,?,?,?,?,?,?,?)",
        (extraction, "tesouro", "cohort", "success", 1, timestamp, timestamp, "run-treasury", "treasury-task", timestamp, 4),
    )
    store.execute_statement(
        "INSERT INTO treasury_cohort_commits (extraction_id,source_payload_sha256,observation_count,reference_min,reference_max,captured_at) VALUES (?,?,?,?,?,?)",
        (extraction, "a" * 64, 4, "2026-10-01", "2026-10-28", timestamp),
    )
    result = admin_client.get("/api/admin/coverage?provider=tesouro")
    assert result.status_code == 200
    body = result.json()
    assert body["coverage_status"] == "metadata_unknown"
    assert body["latest_valid_capture_at"] == timestamp
    assert body["capture_status"] == "recorded"
    assert body["latest_reference"] == "2026-10-28"
    assert body["latest_reference_captured_at"] == timestamp
    assert body["latest_reference_basis"] == "treasury_cohort_commit.reference_max"
    assert body["item_count"] is None and body["items"] == []


def test_sgs_coverage_uses_two_batched_statements_for_full_series_page(
    admin_client: TestClient,
    monkeypatch: pytest.MonkeyPatch,
):
    observed: list[list[tuple[str, tuple[object, ...]]]] = []

    def batch(queries: list[tuple[str, tuple[object, ...]]]):
        observed.append(queries)
        codes = [int(spec.provider_code) for spec in monitoring._coverage_catalog()]
        return [
            [{"series_code": code, "candidate_count": 0, "captured_at": None} for code in codes],
            [{"series_code": code, "candidate_count": 0, "valid_count": 0, "proof_position": None} for code in codes],
        ]

    monkeypatch.setattr(monitoring, "_read_batch", batch)
    response = admin_client.get("/api/admin/coverage?provider=bcb_sgs&limit=100")
    assert response.status_code == 200
    assert len(observed) == 1
    assert len(observed[0]) == 2
    series_count = len(monitoring._coverage_catalog())
    assert observed[0][0][0].count("LIMIT ?") == series_count
    assert observed[0][1][0].count("LIMIT ?") == series_count
    assert len(response.json()["items"]) == series_count


def test_projection_helpers_fail_closed_on_malformed_and_sensitive_legacy_values():
    from fastapi import HTTPException

    assert monitoring._timestamp("not-a-time") is None
    assert monitoring._timestamp("2026-01-01T00:00:00") is None
    assert monitoring._timestamp(42) is None
    assert monitoring._timestamp(datetime(2026, 1, 1, tzinfo=timezone.utc)) == "2026-01-01T00:00:00Z"
    assert monitoring._db_time_expr("created_at") == "julianday(created_at)"
    with pytest.raises(ValueError):
        monitoring._db_time_expr("claim_token")
    assert monitoring._duration("invalid", "2026-01-01T00:00:00Z") is None
    assert monitoring._duration("2026-01-02T00:00:00Z", "2026-01-01T00:00:00Z") == 0
    assert monitoring._safe_state("success-but-unrecognized", monitoring.RunState) == "metadata_unknown"
    assert monitoring._int(True) is None
    assert monitoring._int(-1) is None
    assert monitoring._int(0) == 0
    assert monitoring._safe_error_code("ReadTimeout") == "ReadTimeout"
    assert monitoring._safe_error_code("SECRET_CANARY") == "metadata_unknown"
    assert monitoring._safe_error_code(None) is None

    for cursor in ("!", "x" * (monitoring.MAX_CURSOR + 1), monitoring._cursor_encode({"unexpected": 1})):
        with pytest.raises(HTTPException):
            monitoring._cursor_decode(cursor, {})
    invalid_json = base64.urlsafe_b64encode(b"{").rstrip(b"=").decode()
    with pytest.raises(HTTPException):
        monitoring._cursor_decode(invalid_json, {})
    valid = {"limit": 5, "since": "2025-12-01T00:00:00Z", "until": "2026-01-02T00:00:00Z", "created_at": "2026-01-01T00:00:00Z", "run_id": "safe-id", "anchor": "2026-01-02T00:00:00Z"}
    encoded = monitoring._cursor_encode(valid)
    assert monitoring._cursor_decode(encoded, {"limit": 5})["run_id"] == "safe-id"
    with pytest.raises(HTTPException):
        monitoring._cursor_decode(encoded, {"limit": 6})
    for bad in (
        {**valid, "run_id": "bad id"},
        {**valid, "created_at": "malformed"},
        {**valid, "anchor": "malformed"},
        {**valid, "since": "malformed"},
        {**valid, "until": "malformed"},
    ):
        with pytest.raises(HTTPException):
            monitoring._cursor_decode(monitoring._cursor_encode(bad), {"limit": 5})

    assert monitoring._safe_final_summary(None) == {"status": "not_recorded", "task_count": None}
    assert monitoring._safe_final_summary("x" * (monitoring.MAX_FINAL_JSON_BYTES + 1))["status"] == "not_recorded"
    for malformed in ("{", "[]", '{"status":"green","tasks":[]}', '{"status":"ok","tasks":{}}'):
        assert monitoring._safe_final_summary(malformed)["status"] == "metadata_unknown"
    assert monitoring._safe_final_summary('{"status":"ok","tasks":[]}') == {"status": "ok", "task_count": 0}

    summary = monitoring._run_summary({"run_id": "x" * 200, "cycle_id": "SECRET CANARY", "state": "forged", "created_at": "bad", "manifest_version": "../../secret"})
    assert summary["run_id"] == "metadata_unknown"
    assert summary["cycle_id"] is None
    assert summary["state"] == "metadata_unknown"
    assert summary["manifest_version"] == "metadata_unknown"
    task = monitoring._task_summary({"item_key": "SECRET CANARY", "task": "SECRET CANARY", "status": "forged", "last_error_code": "SECRET_CANARY", "last_error_category": "SECRET_CANARY", "deferral_reason": "SECRET_CANARY"})
    assert task["item_key"] == task["task"] == task["status"] == "metadata_unknown"
    assert task["error_code"] == task["last_error_category"] == task["deferral_reason"] == "metadata_unknown"
    assert monitoring._safe_date(datetime(2026, 2, 3, tzinfo=timezone.utc)) == "2026-02-03"
    assert monitoring._safe_date("not-a-date") is None
    assert monitoring._safe_date(42) is None
    assert monitoring._safe_identifier("abc-12_3:x.y", 32) == "abc-12_3:x.y"
    assert monitoring._safe_identifier("secret value", 32) is None
    assert monitoring._safe_identifier("too-long", 3) is None
    assert monitoring._source_scope("bcb_sgs") == "bcb_sgs:primary:primary"
    assert monitoring._task_counts_from_rows([]) == {}
    counts = monitoring._task_counts_from_rows([
        {"run_id": "r1", "task": "sgs_1", "status": "failed", "total": 2},
        {"run_id": None, "task": "ignored", "status": "failed", "total": 1},
        {"run_id": "r1", "task": None, "status": "failed", "total": 1},
        {"run_id": "r1", "task": "sgs_2", "status": "failed", "total": -1},
    ])
    assert counts == {"r1": {"sgs_1": {"failed": 2}, "sgs_2": {}}}


def test_admin_reads_translate_storage_failures_to_private_no_store_503(admin_client: TestClient, monkeypatch: pytest.MonkeyPatch):
    import sqlite3

    def unavailable(*args, **kwargs):
        raise sqlite3.OperationalError("secret DSN and SQL must not leak")

    monkeypatch.setattr(monitoring, "_read_batch", unavailable)
    for path in ("/api/admin/overview", "/api/admin/providers"):
        response = admin_client.get(path)
        assert response.status_code == 503
        assert response.json() == {"detail": {"error": "admin_data_unavailable"}}
        assert response.headers["cache-control"] == "no-store"
        assert "secret DSN" not in response.text
    monkeypatch.setattr(monitoring, "_read", unavailable)
    for path in ("/api/admin/runs", "/api/admin/coverage?provider=bcb_sgs"):
        if "coverage" in path:
            monkeypatch.setattr(monitoring, "_read_batch", unavailable)
        response = admin_client.get(path)
        assert response.status_code == 503
        assert response.json() == {"detail": {"error": "admin_data_unavailable"}}
        assert response.headers["cache-control"] == "no-store"
    def unavailable_transaction():
        raise sqlite3.OperationalError("private transaction details")

    monkeypatch.setattr(monitoring, "read_transaction", unavailable_transaction)
    response = admin_client.get("/api/admin/runs/known")
    assert response.status_code == 503
    assert response.json() == {"detail": {"error": "admin_data_unavailable"}}
    assert response.headers["cache-control"] == "no-store"


def test_provider_circuit_projection_preserves_recorded_states_without_mutation(admin_client: TestClient, monkeypatch: pytest.MonkeyPatch):
    future = (datetime.now(timezone.utc) + timedelta(days=2)).isoformat()
    rows = [
        {"scope_id": "bcb_sgs:primary:primary", "state": "open", "opened_until": "2000-01-01T00:00:00Z", "retry_not_before": future, "probe_lease_until": None},
        {"scope_id": "focus:primary:primary", "state": "closed", "opened_until": None, "retry_not_before": None, "probe_lease_until": None},
        {"scope_id": "b3:primary:primary", "state": "half_open", "opened_until": "2000-01-01T00:00:00Z", "retry_not_before": None, "probe_lease_until": "2000-01-01T00:00:00Z"},
        {"scope_id": "tesouro:primary:primary", "state": "unexpected", "opened_until": None, "retry_not_before": None, "probe_lease_until": None},
    ]
    monkeypatch.setattr(monitoring, "_read_batch", lambda queries: [rows, [], [{"source": "b3", "error_category": "SECRET_CANARY"}]])
    response = admin_client.get("/api/admin/providers")
    assert response.status_code == 200
    providers = {item["provider"]: item for item in response.json()["items"]}
    assert providers["bcb_sgs"]["circuit"]["recorded_state"] == "open"
    assert providers["bcb_sgs"]["circuit"]["admission_view"] == "temporarily_deferred"
    assert providers["focus"]["circuit"]["admission_view"] == "not_deferred"
    assert providers["b3"]["circuit"]["admission_view"] == "next_admission_reassessment_due"
    assert providers["tesouro"]["circuit"]["recorded_state"] == "metadata_unknown"
    assert providers["b3"]["latest_acquisition_failure_category"] == "metadata_unknown"
    assert "SECRET_CANARY" not in response.text


def test_coverage_bounds_exact_intervals_and_marks_truncation_and_unknown(admin_client: TestClient, monkeypatch: pytest.MonkeyPatch):
    spec = monitoring._coverage_catalog()[0]
    code = int(spec.provider_code)
    old_reference = "2000-01-01"
    captures = [{"series_code": code, "captured_at": "2026-01-01T00:00:00Z", "reference_end": old_reference}]
    captures[0]["candidate_count"] = 1
    proofs = [
        {"series_code": code, "candidate_count": 11, "valid_count": 11, "proof_position": index + 1, "requested_start": "1990-01-01", "requested_end": "1990-12-31", "reference_start": "1990-01-01", "reference_end": "1990-12-31", "captured_at": "2026-01-01T00:00:00Z"}
        for index in range(11)
    ]
    reads = iter((captures, proofs))
    monkeypatch.setattr(monitoring, "_read_batch", lambda _queries: [next(reads), next(reads)])
    monkeypatch.setattr(monitoring, "_availability_metadata", lambda *_args: {"documented_start": "1940-01-01", "first_reference_period": "1940-01-01", "registry_version": "test"})
    response = admin_client.get("/api/admin/coverage", params={"provider": "bcb_sgs", "limit": 1})
    assert response.status_code == 200
    item = response.json()["items"][0]
    assert len(item["acquired_proof_intervals"]) == 10
    assert item["proof_intervals_truncated"] is True
    assert "stale_reference" in item["status_flags"]
    assert item["documented_start"] == "1940-01-01"
    empty = admin_client.get("/api/admin/coverage", params={"provider": "bcb_sgs", "cursor": len(monitoring._coverage_catalog())})
    assert empty.status_code == 200 and empty.json()["items"] == []


def test_coverage_excludes_prestart_references_and_unknown_availability(admin_client: TestClient, monkeypatch: pytest.MonkeyPatch):
    spec = monitoring._coverage_catalog()[0]
    code = int(spec.provider_code)
    monkeypatch.setattr(monitoring, "_read_batch", lambda _queries: [[{"series_code": code, "candidate_count": 1, "captured_at": "2026-01-01T00:00:00Z", "reference_end": "1900-01-01"}], [{"series_code": code, "candidate_count": 0, "valid_count": 0, "proof_position": None}]])
    monkeypatch.setattr(monitoring, "_availability_metadata", lambda *_args: {"documented_start": "1950-01-01", "first_reference_period": "1950-01-01", "registry_version": "test"})
    item = admin_client.get("/api/admin/coverage?provider=bcb_sgs&limit=1").json()["items"][0]
    assert "before_documented_start" in item["status_flags"]
    assert "stale_reference" not in item["status_flags"]
    monkeypatch.setattr(monitoring, "_availability_metadata", lambda *_args: None)
    unknown = admin_client.get("/api/admin/coverage?provider=bcb_sgs&limit=1").json()["items"][0]
    assert "metadata_unknown" in unknown["status_flags"]
    assert "stale_reference" in unknown["status_flags"]


def test_coverage_candidate_budget_marks_unknown_instead_of_false_absence(admin_client: TestClient):
    code = int(monitoring._coverage_catalog()[0].provider_code)
    commits = []
    receipts = []
    for index in range(monitoring._COVERAGE_CANDIDATE_LIMIT + 7):
        token = f"invalid-{index}"
        date_value = f"{2000 + index // 365:04d}-{(index % 365) // 28 + 1:02d}-{(index % 27) + 1:02d}"
        commits.append((token, token, token, token, "f" * 64, code, f"sgs:{code}", "contract-v1", "point", "archive_backfill", "sgs-coverage-policy-v3", date_value, date_value, "a" * 64, "b" * 64, 1, date_value, date_value, f"2026-01-{index % 28 + 1:02d}T00:00:00Z"))
        receipts.append((token, token, token, "bcb_sgs", code, f"sgs:{code}", "contract-v1", "point", "archive_backfill", "sgs-coverage-policy-v3", date_value, date_value, "a" * 64, 1, f"2026-01-{index % 28 + 1:02d}T00:00:00Z", date_value, date_value, token))
    store.execute_many(
        "INSERT INTO sgs_unit_commits (operation_id,extraction_id,run_id,unit_id,plan_sha256,series_code,contract_id,contract_version,identity_kind,scope_role,policy_version,requested_start,requested_end,payload_sha256,canonical_value_sha256,observed_count,reference_start,reference_end,captured_at) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
        commits,
    )
    store.execute_many(
        "INSERT INTO sgs_coverage_receipts (operation_id,run_id,unit_id,source,series_code,contract_id,contract_version,identity_kind,scope_role,policy_version,requested_start,requested_end,payload_sha256,observed_count,captured_at,reference_start,reference_end,business_receipt_id,committed_at) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
        [(*receipt, "2026-01-01T00:00:00Z") for receipt in receipts],
    )
    response = admin_client.get("/api/admin/coverage?provider=bcb_sgs&limit=1")
    assert response.status_code == 200
    item = response.json()["items"][0]
    assert item["capture_search"] == {"status": "metadata_unknown", "candidate_budget": 128, "candidates_examined": 129, "budget_sentinel_present": True}
    assert item["proof_search"] == {"status": "metadata_unknown", "candidate_budget": 128, "candidates_examined": 129, "budget_sentinel_present": True}
    assert item["coverage_status"] == "metadata_unknown"
    assert item["history_status"] == "metadata_unknown"
    assert "history_pending" not in item["status_flags"]
    assert "acquisition_unverified" not in item["status_flags"]
    assert item["acquired_proof_intervals"] == []

    capture_sql, capture_params = monitoring._coverage_capture_query([code])
    proof_sql, proof_params = monitoring._coverage_proof_query([code])
    capture_plan = monitoring._read("EXPLAIN QUERY PLAN " + capture_sql, capture_params)
    proof_plan = monitoring._read("EXPLAIN QUERY PLAN " + proof_sql, proof_params)
    capture_details = "\n".join(str(row.get("detail", "")) for row in capture_plan)
    proof_details = "\n".join(str(row.get("detail", "")) for row in proof_plan)
    print(f"SQLITE_CAPTURE_EXPLAIN_QUERY_PLAN={capture_details}")
    print(f"SQLITE_PROOF_EXPLAIN_QUERY_PLAN={proof_details}")
    assert "sgs_unit_commits_series_capture_idx" in capture_details
    assert "sgs_coverage_receipts_admin_candidates_idx" in proof_details
    assert "MATERIALIZE candidates" in capture_details and "MATERIALIZE candidates" in proof_details
