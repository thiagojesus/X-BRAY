from __future__ import annotations

import json
import os
from collections.abc import Iterator
from threading import local
from urllib.parse import urlparse
from uuid import uuid4

import httpx
import psycopg
import pytest
from fastapi.testclient import TestClient
from psycopg.conninfo import conninfo_to_dict, make_conninfo

import admin_monitoring as monitoring
from admin_auth import require_admin
from db import store
from main import app


@pytest.fixture()
def admin_postgres(monkeypatch: pytest.MonkeyPatch) -> Iterator[TestClient]:
    dsn = os.environ.get("TEST_DATABASE_URL")
    if not dsn:
        pytest.skip("requires disposable PostgreSQL 17 TEST_DATABASE_URL")
    parsed = conninfo_to_dict(dsn)
    if not parsed.get("dbname", "").endswith("_test") or urlparse(dsn).hostname not in {"127.0.0.1", "localhost", "::1"}:
        pytest.fail("TEST_DATABASE_URL must be an isolated loopback *_test database")
    with psycopg.connect(dsn, autocommit=True) as admin:
        version, = admin.execute("SELECT current_setting('server_version_num')::int").fetchone()
        if version < 170000 or version >= 180000:
            pytest.fail("admin monitoring integration requires PostgreSQL 17")
        schema = f"admin_monitoring_{uuid4().hex}"
        admin.execute(f'CREATE SCHEMA "{schema}"')
    monkeypatch.setattr(store, "DATABASE_URL", make_conninfo(dsn, options=f"-c search_path={schema}"))
    monkeypatch.setattr(store, "_pg_local", local())
    store.init_db()
    app.dependency_overrides[require_admin] = lambda: {"user_id": "synthetic-human"}
    with TestClient(app) as client:
        try:
            yield client
        finally:
            app.dependency_overrides.pop(require_admin, None)
            connection = getattr(store._pg_local, "conn", None)
            if connection is not None:
                connection.close()
            with psycopg.connect(dsn, autocommit=True) as admin:
                admin.execute(f'DROP SCHEMA "{schema}" CASCADE')
            monkeypatch.setattr(store, "_pg_local", local())


def test_every_admin_read_projection_executes_on_postgres_without_business_writes(
    admin_postgres: TestClient,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    run_id = f"pg-{uuid4().hex}"
    timestamp = "2026-06-01T12:00:00+00:00"
    store._execute(
        "INSERT INTO refresh_runs (run_id, cycle_id, state, force, manifest_version, created_at, updated_at, finished_at, final_result) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (run_id, "cycle-pg", "partial", 0, "daily-refresh-v6", timestamp, timestamp, timestamp, '{"status":"partial","tasks":[]}'),
    )
    tied_run_id = f"zz-{uuid4().hex}"
    store._execute(
        "INSERT INTO refresh_runs (run_id, cycle_id, state, force, manifest_version, created_at, updated_at, finished_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
        (tied_run_id, "cycle-tied", "succeeded", 0, "daily-refresh-v6", "2026-06-01T09:00:00-03:00", timestamp, timestamp),
    )
    store._execute(
        "INSERT INTO refresh_work_items (run_id,item_key,task,task_order,status,attempt,created_at,updated_at,finished_at,series_updated,record_count,claim_generation,acquisition_attempts) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)",
        (run_id, "item-pg", "sgs_123", 0, "failed", 1, timestamp, timestamp, timestamp, 0, 0, 1, 1),
    )
    store._execute(
        "INSERT INTO source_access_state (scope_id,state,opened_until,updated_at) VALUES (?,?,?,?)",
        ("bcb_sgs:primary:primary", "open", "2000-01-01T00:00:00Z", timestamp),
    )
    before = {
        "runs": store.query_rows("SELECT COUNT(*) AS n FROM refresh_runs")[0]["n"],
        "items": store.query_rows("SELECT COUNT(*) AS n FROM refresh_work_items")[0]["n"],
        "access": store.query_rows("SELECT state, opened_until FROM source_access_state ORDER BY scope_id"),
        "extractions": store.query_rows("SELECT COUNT(*) AS n FROM source_extractions")[0]["n"],
        "commits": store.query_rows("SELECT COUNT(*) AS n FROM sgs_unit_commits")[0]["n"],
        "coverage_receipts": store.query_rows("SELECT COUNT(*) AS n FROM sgs_coverage_receipts")[0]["n"],
        "acquisition_commits": store.query_rows("SELECT COUNT(*) AS n FROM source_acquisition_commits")[0]["n"],
        "treasury_commits": store.query_rows("SELECT COUNT(*) AS n FROM treasury_cohort_commits")[0]["n"],
    }

    original_send = httpx.Client.send

    def no_provider_network(client: httpx.Client, request: httpx.Request, *args: object, **kwargs: object) -> httpx.Response:
        if request.url.host != "testserver":
            raise AssertionError("admin read endpoint attempted provider HTTP")
        return original_send(client, request, *args, **kwargs)

    monkeypatch.setattr(httpx.Client, "send", no_provider_network)
    responses = [
        admin_postgres.get("/api/admin/overview"),
        admin_postgres.get("/api/admin/runs?limit=10&since=2026-01-01T00:00:00Z&until=2027-01-01T00:00:00Z"),
        admin_postgres.get(f"/api/admin/runs/{run_id}"),
        admin_postgres.get("/api/admin/providers"),
        admin_postgres.get("/api/admin/coverage?provider=bcb_sgs&limit=2"),
    ]
    assert [response.status_code for response in responses] == [200] * 5
    assert all(response.headers["cache-control"] == "no-store" for response in responses)
    assert responses[2].json()["run"]["run_id"] == run_id
    assert responses[2].json()["run"]["recorded_final_summary"] == {"status": "partial", "task_count": 0}
    history_ids = [item["run_id"] for item in responses[1].json()["items"]]
    assert history_ids.index(tied_run_id) < history_ids.index(run_id)
    current_circuit = next(item for item in responses[3].json()["items"] if item["provider"] == "bcb_sgs")
    assert current_circuit["circuit"]["recorded_state"] == "open"
    assert current_circuit["circuit"]["admission_view"] == "next_admission_reassessment_due"
    assert "claim_token" not in "".join(response.text for response in responses)
    after = {
        "runs": store.query_rows("SELECT COUNT(*) AS n FROM refresh_runs")[0]["n"],
        "items": store.query_rows("SELECT COUNT(*) AS n FROM refresh_work_items")[0]["n"],
        "access": store.query_rows("SELECT state, opened_until FROM source_access_state ORDER BY scope_id"),
        "extractions": store.query_rows("SELECT COUNT(*) AS n FROM source_extractions")[0]["n"],
        "commits": store.query_rows("SELECT COUNT(*) AS n FROM sgs_unit_commits")[0]["n"],
        "coverage_receipts": store.query_rows("SELECT COUNT(*) AS n FROM sgs_coverage_receipts")[0]["n"],
        "acquisition_commits": store.query_rows("SELECT COUNT(*) AS n FROM source_acquisition_commits")[0]["n"],
        "treasury_commits": store.query_rows("SELECT COUNT(*) AS n FROM treasury_cohort_commits")[0]["n"],
    }
    assert after == before


def test_coverage_candidate_queries_stop_at_indexed_per_series_budget_on_postgres(
    admin_postgres: TestClient,
) -> None:
    codes = [int(spec.provider_code) for spec in monitoring._coverage_catalog()[:3]]
    commit_rows = []
    receipt_rows = []
    row_count = 1500
    for code in codes:
        for index in range(row_count):
            ident = f"candidate-{code}-{index}"
            requested = f"{2000 + index // 360:04d}-{index % 12 + 1:02d}-01"
            captured = f"2026-01-{index % 28 + 1:02d}T00:00:{index % 60:02d}Z"
            commit_rows.append((ident, ident, ident, ident, "f" * 64, code, f"sgs:{code}", "contract-v1", "point", "archive_backfill", "sgs-coverage-policy-v3", requested, requested, "a" * 64, "b" * 64, 1, requested, requested, captured))
            receipt_rows.append((ident, ident, ident, "bcb_sgs", code, f"sgs:{code}", "contract-v1", "point", "archive_backfill", "sgs-coverage-policy-v3", requested, requested, "a" * 64, 1, captured, requested, requested, ident, captured))
    connection = store._get_pg_conn()
    with connection.transaction():
        with connection.cursor() as cursor:
            cursor.executemany(
                "INSERT INTO sgs_unit_commits (operation_id,extraction_id,run_id,unit_id,plan_sha256,series_code,contract_id,contract_version,identity_kind,scope_role,policy_version,requested_start,requested_end,payload_sha256,canonical_value_sha256,observed_count,reference_start,reference_end,captured_at) VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)",
                commit_rows,
            )
            cursor.executemany(
                "INSERT INTO sgs_coverage_receipts (operation_id,run_id,unit_id,source,series_code,contract_id,contract_version,identity_kind,scope_role,policy_version,requested_start,requested_end,payload_sha256,observed_count,captured_at,reference_start,reference_end,business_receipt_id,committed_at) VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)",
                receipt_rows,
            )
        connection.execute("ANALYZE sgs_unit_commits")
        connection.execute("ANALYZE sgs_coverage_receipts")

    response = admin_postgres.get("/api/admin/coverage?provider=bcb_sgs&limit=3")
    assert response.status_code == 200
    items = response.json()["items"]
    assert len(items) == 3
    assert all(item["capture_search"]["candidates_examined"] == 129 for item in items)
    assert all(item["proof_search"]["candidates_examined"] == 129 for item in items)
    assert all(item["coverage_status"] == "metadata_unknown" for item in items)

    def explain_analyze(query: tuple[str, tuple[object, ...]]) -> dict[str, object]:
        sql, params = query
        result = connection.execute(
            "EXPLAIN (ANALYZE, BUFFERS, FORMAT JSON) " + sql.replace("?", "%s"), params,
        ).fetchone()["QUERY PLAN"]
        return result[0]["Plan"]

    def nodes(plan: dict[str, object]) -> list[dict[str, object]]:
        result = [plan]
        for child in plan.get("Plans", []):
            result.extend(nodes(child))
        return result

    for plan_query, index_name in (
        (monitoring._coverage_capture_query(codes), "sgs_unit_commits_series_capture_idx"),
        (monitoring._coverage_proof_query(codes), "sgs_coverage_receipts_admin_candidates_idx"),
    ):
        plan_nodes = nodes(explain_analyze(plan_query))
        candidate_index_scans = [node for node in plan_nodes if node.get("Index Name") == index_name]
        limits = [node for node in plan_nodes if node.get("Node Type") == "Limit"]
        assert candidate_index_scans
        assert len(limits) >= len(codes)
        assert all(node.get("Actual Rows", row_count + 1) <= 129 for node in limits)
        assert all(node.get("Actual Loops", 2) == 1 for node in limits)
        assert sum(int(node.get("Actual Rows", 0)) for node in candidate_index_scans) <= 129 * len(codes)
        print(json.dumps({
            "EXPLAIN_ANALYZE": index_name,
            "candidate_index_scans": [{"type": node.get("Node Type"), "actual_rows": node.get("Actual Rows"), "loops": node.get("Actual Loops")} for node in candidate_index_scans],
            "limit_nodes": [{"actual_rows": node.get("Actual Rows"), "loops": node.get("Actual Loops")} for node in limits],
        }, sort_keys=True))


def test_coverage_requires_exact_receipt_lineage_and_marks_budget_unknown_on_postgres(
    admin_postgres: TestClient,
) -> None:
    from dataclasses import replace
    from datetime import date, datetime, timezone

    from db.sgs_refresh_plan import (
        SGSPlanBinding,
        SGSUnitCommitReceipt,
        build_sgs_plan,
        canonical_sgs_values,
        freeze_sgs_plan,
        plan_digest,
        reconcile_sgs_unit_commit,
    )

    plan = build_sgs_plan(date(2026, 10, 27), max_archive_units=1)
    run_id = f"pg-admin-proof-{uuid4().hex}"
    freeze_sgs_plan(run_id, plan)
    unit = next(item for item in plan.units if item.role != "recent")
    values = [{"data": unit.start_date.strftime("%d/%m/%Y"), "valor": "7.25"}]
    value_hash, count, reference_start, reference_end = canonical_sgs_values(unit.series_code, values)
    captured = datetime.now(timezone.utc)
    commit = SGSUnitCommitReceipt(
        operation_id=f"proof-{uuid4().hex}", extraction_id="", run_id=run_id,
        unit_id=unit.unit_id, plan_sha256=plan_digest(plan), series_code=unit.series_code,
        contract_id=unit.contract_id, contract_version=unit.contract_version,
        identity_kind=unit.identity_kind, scope_role=unit.role, policy_version=unit.policy_version,
        requested_start=unit.start_date, requested_end=unit.end_date,
        payload_sha256="a" * 64, canonical_value_sha256=value_hash, observed_count=count,
        reference_start=reference_start, reference_end=reference_end, captured_at=captured,
    )
    commit = replace(commit, extraction_id=commit.operation_id)
    store.upsert_sgs(unit.series_code, values, first_seen_at=captured.isoformat(), sgs_unit_commit_receipt=commit)
    assert reconcile_sgs_unit_commit(SGSPlanBinding(run_id, plan_digest(plan), unit)) == commit

    # Corrupt otherwise-nearby receipt copies: one hash mismatch, one unit-window mismatch.
    valid = store.query_rows("SELECT * FROM sgs_coverage_receipts WHERE operation_id = ?", (commit.operation_id,))[0]
    columns = tuple(valid)
    insert_sql = f"INSERT INTO sgs_coverage_receipts ({','.join(columns)}) VALUES ({','.join('?' for _ in columns)})"
    for suffix, updates in (
        ("hash", {"operation_id": f"{commit.operation_id}-bad-hash", "payload_sha256": "b" * 64}),
        ("window", {"operation_id": f"{commit.operation_id}-bad-window", "requested_start": "1900-01-01"}),
    ):
        forged = {**valid, **updates}
        store._execute(insert_sql, tuple(forged[column] for column in columns))

    response = admin_postgres.get("/api/admin/coverage?provider=bcb_sgs&limit=100")
    assert response.status_code == 200
    item = next(row for row in response.json()["items"] if row["series_code"] == unit.series_code)
    assert item["proof_search"]["candidates_examined"] == 3
    assert item["proof_search"]["status"] == "complete"
    assert item["acquired_proof_intervals"] == [{
        "requested_start": unit.start_date.isoformat(), "requested_end": unit.end_date.isoformat(),
        "reference_start": reference_start.isoformat(), "reference_end": reference_end.isoformat(),
        "captured_at": captured.isoformat().replace("+00:00", "Z"),
        "basis": "exact_validated_sgs_archive_receipt",
    }]

    # Bring this series to its existing bounded candidate cap. The valid interval remains
    # visible, but the endpoint must not claim a complete receipt history.
    connection = store._get_pg_conn()
    extra = []
    for index in range(monitoring.COVERAGE_CANDIDATE_BUDGET + 1 - 3):
        forged = dict(valid)
        forged.update(operation_id=f"zz-{commit.operation_id}-overflow-{index}", payload_sha256="c" * 64)
        extra.append(tuple(forged[column] for column in columns))
    with connection.transaction(), connection.cursor() as cursor:
        cursor.executemany(insert_sql.replace("?", "%s"), extra)
    overflow = admin_postgres.get("/api/admin/coverage?provider=bcb_sgs&limit=100")
    assert overflow.status_code == 200
    capped = next(row for row in overflow.json()["items"] if row["series_code"] == unit.series_code)
    assert capped["proof_search"]["candidates_examined"] == monitoring.COVERAGE_CANDIDATE_BUDGET + 1
    assert capped["proof_search"]["status"] == "metadata_unknown"
    assert capped["coverage_status"] == "metadata_unknown"
    assert capped["history_status"] == "metadata_unknown"
    assert capped["acquired_proof_intervals"]
