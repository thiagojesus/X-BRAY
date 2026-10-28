from __future__ import annotations

import hashlib
import json
import os
from datetime import datetime, timezone, tzinfo
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from threading import Thread
from typing import NoReturn
from urllib.parse import urlparse
from uuid import uuid4

import psycopg
import pytest
from fastapi.testclient import TestClient

AS_OF = datetime(2026, 10, 28, 17, 54, 12, tzinfo=timezone.utc)
CAPTURED = datetime(2026, 10, 28, 1, 30, tzinfo=timezone.utc)
ACCEPTED_CODES = (
    4639, 24363, 24364, 24382, 29602, 29604, 29606, 29608,
    4513, 5727, 5760, 5793, 13762, 1788, 1835, 27788, 27810,
)
STALE_CODES = (20716, 21084, 25353, 29034, 29037)


def _snapshot(dsn: str, evidence_run: str, status_run: str) -> str:
    queries = (
        ("sgs", "SELECT series_code, date, value, fetched_at FROM sgs ORDER BY series_code, date"),
        ("vintages", "SELECT provider, series_key, reference_period, raw_value_json, source_published_at, first_seen_at, available_at, payload_hash, revision_id FROM observation_vintages WHERE provider = 'bcb_sgs' ORDER BY series_key, reference_period"),
        ("commits", "SELECT run_id, unit_id, series_code, extraction_id, payload_sha256, canonical_value_sha256, captured_at, reference_start, reference_end FROM sgs_unit_commits WHERE run_id = %s ORDER BY unit_id"),
        ("coverage_receipts", "SELECT operation_id, run_id, unit_id, series_code, payload_sha256, captured_at, reference_start, reference_end FROM sgs_coverage_receipts WHERE run_id = %s ORDER BY operation_id"),
        ("plans", "SELECT run_id, plan_version, plan_sha256, plan_json FROM sgs_refresh_plans WHERE run_id IN (%s, %s) ORDER BY run_id"),
        ("plan_units", "SELECT run_id, unit_id, role, status, freshness_days FROM sgs_refresh_plan_units WHERE run_id IN (%s, %s) ORDER BY run_id, unit_id"),
        ("runs", "SELECT run_id, state, final_result, manifest_version FROM refresh_runs WHERE run_id IN (%s, %s) ORDER BY run_id"),
        ("work_items", "SELECT run_id, item_key, status, claimed_at, finished_at, series_updated, record_count, error_code FROM refresh_work_items WHERE run_id IN (%s, %s) ORDER BY run_id, item_key"),
        ("extractions", "SELECT extraction_id, run_id, source, indicator, status, network_called, request_count, received_count, valid_count, persisted_count, started_at, finished_at, network_provider, network_route, network_contract, network_scope_id, network_identity, network_coverage, network_payload_sha256, network_captured_at, network_reference_count FROM source_extractions WHERE source = 'bcb_sgs' ORDER BY extraction_id"),
    )
    snapshots: list[object] = []
    with psycopg.connect(dsn) as connection:
        for name, sql in queries:
            params = (evidence_run,) if name in {"commits", "coverage_receipts"} else (evidence_run, status_run) if name in {"plans", "plan_units", "runs", "work_items"} else ()
            snapshots.append((name, connection.execute(sql, params).fetchall()))
    return hashlib.sha256(json.dumps(snapshots, default=str, sort_keys=True).encode()).hexdigest()


def test_native_postgres_refresh_status_get_preserves_real_22_series_fixture(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    dsn = os.environ.get("TEST_DATABASE_URL", "")
    parsed = urlparse(dsn)
    if parsed.hostname not in {"127.0.0.1", "localhost", "::1"} or parsed.path != "/xbray_test":
        pytest.fail("native reference freshness test requires local PostgreSQL xbray_test")

    import provider_http
    import refresh_reporting
    import source_ingestion
    from datafetchers import bcb_sgs
    from db import store
    from db.sgs_refresh_plan import (
        SGSUnitCommitReceipt,
        build_sgs_plan,
        canonical_sgs_values,
        freeze_sgs_plan,
        plan_digest,
        plan_item_task_name,
    )
    from main import app
    from tests.test_refresh_status import _persist_refresh_run

    with psycopg.connect(dsn) as connection:
        version, database = connection.execute(
            "SELECT version(), current_database()"
        ).fetchone()
        assert version.startswith("PostgreSQL 17")
        assert database == "xbray_test"
    if store._conn is not None:
        store._conn.close()
    store._conn = None
    monkeypatch.setattr(store, "DATABASE_URL", dsn)
    if getattr(store._pg_local, "conn", None) is not None:
        store._pg_local.conn.close()
    store._pg_local.conn = None
    store.init_db()

    plan = build_sgs_plan(AS_OF.date(), max_archive_units=0)
    by_code = {unit.series_code: unit for unit in plan.units if unit.role == "recent"}
    assert { *ACCEPTED_CODES, *STALE_CODES } <= set(by_code)
    evidence_run = f"fresh-proof-{uuid4().hex}"
    status_run = f"fresh-status-{uuid4().hex}"
    tasks = [plan_item_task_name(unit) for unit in plan.units]
    _persist_refresh_run(
        run_id=evidence_run, manifest_version="daily-refresh-v6", tasks=tasks,
        started_at=datetime(2026, 10, 28, 1, 0, tzinfo=timezone.utc),
    )
    freeze_sgs_plan(evidence_run, plan, CAPTURED)

    reference_by_code = {
        code: "01/07/2026" if code in {29034, 29037} else "01/08/2026"
        for code in ACCEPTED_CODES + STALE_CODES
    }
    for unit in plan.units:
        if unit.series_code == 226:
            reference_by_code[unit.series_code] = "28/10/2026"
        elif unit.series_code not in reference_by_code:
            reference_by_code[unit.series_code] = {
                "annual": "01/01/2025",
                "quarterly": "01/09/2026",
            }.get(unit.frequency, "01/10/2026")

    class FixtureHandler(BaseHTTPRequestHandler):
        calls = 0

        def do_GET(self) -> None:
            type(self).calls += 1
            code = int(self.path.split("/")[-1].split("?")[0])
            record: dict[str, str] = {"data": reference_by_code[code], "valor": "1.25"}
            if code == 226:
                record["dataFim"] = "26/11/2026"
            payload = json.dumps([record]).encode()
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(payload)))
            self.end_headers()
            self.wfile.write(payload)

        def log_message(self, _format: str, *_args: object) -> None:
            return

    server = ThreadingHTTPServer(("127.0.0.1", 0), FixtureHandler)
    server.daemon_threads = True
    thread = Thread(target=server.serve_forever, daemon=True)
    thread.start()
    local_base = f"http://127.0.0.1:{server.server_port}/sgs/{{code}}"
    monkeypatch.setattr(bcb_sgs, "BCB_SGS_BASE", local_base)
    monkeypatch.setattr(provider_http, "require_allowed_provider_url", lambda _url: _url)
    monkeypatch.setattr(store, "_utc_now", lambda: CAPTURED.isoformat())
    class CapturedDatetime(datetime):
        @classmethod
        def now(cls, tz: tzinfo | None = None) -> datetime:
            return CAPTURED if tz else CAPTURED.replace(tzinfo=None)

    monkeypatch.setattr(bcb_sgs, "datetime", CapturedDatetime)
    monkeypatch.setattr(source_ingestion, "datetime", CapturedDatetime)
    for unit in plan.units:
        code = unit.series_code
        reference = reference_by_code[code]
        prior_extractions = {
            row["extraction_id"]
            for row in store._query(
                "SELECT extraction_id FROM source_extractions WHERE source = ? AND indicator = ?",
                ("bcb_sgs", str(code)),
            )
        }
        records = bcb_sgs.fetch_sgs_series(
            code, start_date=reference, end_date=reference, use_cache=False, fail_fast=True
        )
        assert len(records) == 1
        new_extractions = store._query(
            "SELECT extraction_id, network_payload_sha256 FROM source_extractions "
            "WHERE source = ? AND indicator = ? AND network_payload_sha256 IS NOT NULL",
            ("bcb_sgs", str(code)),
        )
        extraction = next(
            row for row in new_extractions if row["extraction_id"] not in prior_extractions
        )
        payload_hash, count, reference_start, reference_end = canonical_sgs_values(code, records)
        receipt = SGSUnitCommitReceipt(
            operation_id=extraction["extraction_id"], extraction_id=extraction["extraction_id"],
            run_id=evidence_run, unit_id=unit.unit_id, plan_sha256=plan_digest(plan),
            series_code=code, contract_id=unit.contract_id,
            contract_version=unit.contract_version, identity_kind=unit.identity_kind,
            scope_role=unit.role, policy_version=unit.policy_version,
            requested_start=unit.start_date, requested_end=unit.end_date,
            payload_sha256=extraction["network_payload_sha256"], canonical_value_sha256=payload_hash,
            observed_count=count, reference_start=reference_start, reference_end=reference_end,
            captured_at=CAPTURED,
        )
        if code == 226:
            store.upsert_tr_periods(
                records, first_seen_at=CAPTURED.isoformat(), sgs_unit_commit_receipt=receipt,
            )
        else:
            store.upsert_sgs(
                code, records, first_seen_at=CAPTURED.isoformat(),
                sgs_unit_commit_receipt=receipt,
            )
    server.shutdown()
    server.server_close()
    thread.join(timeout=2)
    assert FixtureHandler.calls == len(plan.units)

    _persist_refresh_run(
        run_id=status_run, manifest_version="daily-refresh-v6", tasks=tasks,
        started_at=datetime(2099, 1, 1, tzinfo=timezone.utc),
    )
    freeze_sgs_plan(status_run, plan, CAPTURED)
    before = _snapshot(dsn, evidence_run, status_run)

    class AsOfDatetime(datetime):
        @classmethod
        def now(cls, tz: tzinfo | None = None) -> datetime:
            return AS_OF if tz else AS_OF.replace(tzinfo=None)

    monkeypatch.setattr(refresh_reporting, "datetime", AsOfDatetime)

    query_count = 0
    original_query = store._query

    def counted_query(sql: str, params=()):
        nonlocal query_count
        query_count += 1
        return original_query(sql, params)

    def forbidden_write(*_args: object, **_kwargs: object) -> NoReturn:
        raise AssertionError("refresh-status GET must remain read-only")

    monkeypatch.setattr(store, "_query", counted_query)
    monkeypatch.setattr(store, "_execute", forbidden_write)
    monkeypatch.setattr(store, "execute_statement", forbidden_write)
    monkeypatch.setattr(store, "execute_transaction", forbidden_write)
    monkeypatch.setattr(provider_http, "get_provider_response", forbidden_write)
    monkeypatch.setattr(bcb_sgs, "_fetch_sgs_json", forbidden_write)
    with TestClient(app) as client:
        first = client.get("/api/refresh-status")
        second = client.get("/api/refresh-status")
    assert first.status_code == second.status_code == 200
    payload = first.json()
    group_tasks = {task["task"]: task for task in payload["tasks"]}
    all_groups = [
        task for task in group_tasks.values()
        if task.get("series_reference_freshness_details")
    ]
    details = {
        code: detail
        for task in all_groups
        for code, detail in task["series_reference_freshness_details"].items()
    }
    statuses = {
        code: value
        for task in all_groups
        for code, value in task["series_reference_freshness"].items()
    }
    assert set(details) >= {str(code) for code in ACCEPTED_CODES + STALE_CODES}
    assert all(statuses[str(code)] == "ok" for code in ACCEPTED_CODES)
    assert all(statuses[str(code)] == "stale" for code in STALE_CODES)
    assert details["4639"]["observed_reference"] == "2026-08-01"
    assert details["4639"]["basis_date"] == "2026-08-31"
    assert details["4639"]["deadline"] == "2026-11-14"
    assert details["4639"]["as_of_date"] == "2026-10-28"
    assert details["4639"]["policy_version"] == "reference-freshness-v2"
    assert details["20716"]["deadline"] == "2026-10-15"
    assert details["29034"]["observed_reference"] == "2026-07-01"
    assert details["29034"]["deadline"] == "2026-10-14"
    assert all(
        details[str(code)]["observed_reference"] != details[str(code)]["basis_date"]
        for code in ACCEPTED_CODES + STALE_CODES
    )
    assert all(
        task["last_data_captured_at"] == CAPTURED.isoformat()
        for task in all_groups
    )
    assert all(task["series_network_refreshed"] == 0 for task in all_groups)
    assert all(task["acquisition_status"] == "cached_or_unverified" for task in all_groups)
    assert payload["sgs_incremental"]["recent"]["network_refreshed_units"] == 0
    assert query_count < 15
    assert all(task["data_status"] == "ok" for task in all_groups)
    assert payload["overall_status"] == "partial"
    assert _snapshot(dsn, evidence_run, status_run) == before

    conn = getattr(store._pg_local, "conn", None)
    if conn is not None:
        conn.close()
        store._pg_local.conn = None
