import hashlib
import json
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


def test_acquisition_diagnostic_query_reads_only_bounded_run_rows(isolated_database: None) -> None:
    import json

    from db.store import (
        acquisition_diagnostics_for_refresh_run,
        finish_source_extraction,
        start_source_extraction,
    )

    trace = json.dumps([{
        "dispatch_sequence": 1,
        "dispatch_started_at": "2026-10-29T00:00:00+00:00",
        "dispatch_finished_at": "2026-10-29T00:00:01+00:00",
        "diagnostics": {
            "version": "source-request-diagnostics-v1", "phase": "request",
            "failure_code": "read_timeout", "response": {"status_code": None, "content_type": "unknown", "body_kind": "unknown"},
        },
    }])
    for run_id, source in (("diag-run", "b3"), ("other-run", "bcb_sgs")):
        extraction_id = f"{run_id}-extraction"
        start_source_extraction({
            "extraction_id": extraction_id, "source": source, "indicator": "di_curve",
            "qualifiers_json": "{}", "status": "running", "network_called": 1,
            "started_at": "2026-10-29T00:00:00+00:00", "run_id": run_id,
            "task_key": "b3_curve", "cycle_id": run_id,
        })
        finish_source_extraction({
            "extraction_id": extraction_id, "status": "network_error", "network_called": 1,
            "finished_at": "2026-10-29T00:00:01+00:00", "request_trace_json": trace,
            "error_code": "RuntimeError", "error_category": "network_timeout",
        })

    result = acquisition_diagnostics_for_refresh_run("diag-run")
    assert result["truncated"] is False
    assert [row["extraction_id"] for row in result["rows"]] == ["diag-run-extraction"]
    with pytest.raises(ValueError, match="invalid refresh run id"):
        acquisition_diagnostics_for_refresh_run("")


def test_refresh_status_http_projects_persisted_sgs_b3_and_legacy_diagnostics_read_only(
    isolated_database: None,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import json

    from db.store import finish_source_extraction, start_source_extraction
    from main import app

    run_id = "diagnostic-http-run"
    tasks = ["sgs_juros", "b3_curve", "sgs_ipca_grupos", "unsupported", "malformed"]
    _persist_refresh_run(run_id=run_id, manifest_version="daily-refresh-v2", tasks=tasks)
    trace_sgs = json.dumps([{
        "dispatch_sequence": 1,
        "dispatch_started_at": "2026-10-29T00:00:00+00:00",
        "dispatch_finished_at": "2026-10-29T00:00:01+00:00",
        "diagnostics": {
            "version": "source-request-diagnostics-v1", "phase": "decode",
            "failure_code": "invalid_json_shape", "duration_ms": 1000.0,
            "response": {
                "status_code": 200, "content_type": "application/json", "body_kind": "object",
                "bytes_received": 11, "body_sha256": "a" * 64,
            },
            "request_context": {
                "provider": "bcb_sgs", "series_code": 433,
                "requested_start": "2026-01-01", "requested_end": "2026-01-31",
                "expected_kind": "list", "local_attempt": 1,
            },
        },
        "url": "https://private.invalid/?token=DO_NOT_REPORT",
    }])
    trace_b3 = json.dumps([{
        "dispatch_sequence": 1,
        "dispatch_started_at": "2026-10-29T00:00:00+00:00",
        "dispatch_finished_at": "2026-10-29T00:00:02+00:00",
        "diagnostics": {
            "version": "source-request-diagnostics-v1", "phase": "response_read",
            "failure_code": "read_timeout", "duration_ms": 2000.0,
            "response": {"status_code": None, "content_type": "unknown", "body_kind": "unknown"},
            "request_context": {
                "provider": "b3", "trade_date": "2026-10-29",
                "file_name": "SPRD261029.zip", "local_attempt": 2,
            },
        },
        "headers": {"Authorization": "DO_NOT_REPORT"},
    }])
    records = [
        ("sgs", "bcb_sgs", "433", "sgs_juros", "validation_error", "InvalidSGSPayloadError", "invalid_schema", "expected-list", trace_sgs),
        ("b3", "b3", "di_curve", "b3_curve", "network_error", "RuntimeError", "network_timeout", "RuntimeError", trace_b3),
        ("old", "bcb_sgs", "434", "sgs_ipca_grupos", "network_error", "RuntimeError", "network_timeout", "RuntimeError", None),
        ("unsupported", "b3", "unsupported", "unsupported", "network_error", "RuntimeError", "network_timeout", "RuntimeError", '[{"diagnostics":{"version":"future-v9"}}]'),
        ("malformed", "b3", "malformed", "malformed", "network_error", "RuntimeError", "network_timeout", "RuntimeError", "{not-json"),
    ]
    for suffix, source, indicator, task_key, status, error_code, error_category, error_detail, trace in records:
        extraction_id = f"{run_id}-{suffix}"
        start_source_extraction({
            "extraction_id": extraction_id, "source": source, "indicator": indicator,
            "qualifiers_json": "{}", "status": "running", "network_called": 1,
            "started_at": "2026-10-29T00:00:00+00:00", "run_id": run_id,
            "task_key": task_key, "cycle_id": run_id,
        })
        finish_source_extraction({
            "extraction_id": extraction_id, "status": status, "network_called": 1,
            "finished_at": "2026-10-29T00:00:02+00:00", "error_code": error_code,
            "error_category": error_category, "error_detail": error_detail,
            "request_trace_json": trace,
        })

    from db import store

    original_query = store._query
    diagnostic_selects: list[str] = []

    def counted_query(sql: str, params: list | tuple = ()) -> list[dict]:
        if "request_trace_json" in sql and "source IN" in sql:
            diagnostic_selects.append(sql)
        return original_query(sql, params)

    monkeypatch.setattr(store, "_query", counted_query)

    def database_fingerprint() -> dict[str, list[dict[str, object]]]:
        tables = store._query(
            "SELECT name FROM sqlite_master WHERE type = 'table' AND name NOT LIKE 'sqlite_%' ORDER BY name"
        )
        return {
            str(row["name"]): store._query(f'SELECT * FROM "{str(row["name"]).replace(chr(34), chr(34) * 2)}"')
            for row in tables
        }

    before = database_fingerprint()
    with TestClient(app, raise_server_exceptions=False) as client:
        first = client.get("/api/refresh-status")
        second = client.get("/api/refresh-status")
    after = database_fingerprint()

    assert first.status_code == second.status_code == 200
    payload = first.json()
    summary = payload["acquisition_diagnostics"]
    assert summary["schema_version"] == "acquisition-diagnostics-v1"
    assert summary["run_id"] == run_id
    assert set(summary) == {"schema_version", "run_id", "status", "items", "truncated_items", "truncated_rows"}
    assert summary["status"] == "recorded"
    by_task = {item["task_key"]: item for item in summary["items"]}
    assert by_task["sgs_juros"]["validation_reason"] == "expected_list"
    assert by_task["sgs_juros"]["requests"][0]["response"] == {
        "status_code": 200, "content_type": "application/json", "body_kind": "object", "bytes_received": 11,
    }
    assert by_task["sgs_juros"]["request_context"]["series_code"] == 433
    assert by_task["b3_curve"]["requests"][0]["failure_code"] == "read_timeout"
    assert by_task["b3_curve"]["request_context"] == {
        "provider": "b3", "trade_date": "2026-10-29", "file_name": "SPRD261029.zip", "local_attempt": 2,
    }
    assert by_task["sgs_ipca_grupos"]["diagnostics_status"] == "not_recorded"
    assert by_task["unsupported"]["diagnostics_status"] == "unsupported_version"
    assert by_task["malformed"]["diagnostics_status"] == "invalid"
    assert "DO_NOT_REPORT" not in first.text
    second_payload = second.json()
    assert payload["acquisition_diagnostics"] == second_payload["acquisition_diagnostics"]
    assert payload["tasks"] == second_payload["tasks"]
    assert payload["overall_status"] == second.json()["overall_status"]
    assert before == after
    assert len(diagnostic_selects) == 2


@pytest.mark.parametrize(
    ("body", "body_kind", "error_detail", "validation_reason"),
    [
        (b'{"a":12345}', "object", "expected-list", "expected_list"),
        (
            b'[{"data":"01/01/2026","valor":"not-numeric"}]',
            "list", "invalid-value", "invalid_value",
        ),
    ],
)
def test_actual_sgs_http_200_validation_failure_persists_and_projects_without_transport_failure(
    isolated_database: None,
    monkeypatch: pytest.MonkeyPatch,
    body: bytes,
    body_kind: str,
    error_detail: str,
    validation_reason: str,
) -> None:
    import httpx

    from datafetchers import bcb_sgs
    from db import store
    from main import app
    from source_ingestion import reset_refresh_context, set_refresh_context

    run_id = "actual-sgs-json-shape"
    _persist_refresh_run(run_id=run_id, manifest_version="daily-refresh-v2", tasks=["sgs_juros"])
    monkeypatch.setattr(bcb_sgs, "get_sgs_range", lambda _code: (None, None))
    monkeypatch.setattr(bcb_sgs, "query_sgs", lambda *_args: [])
    monkeypatch.setattr(bcb_sgs, "upsert_sgs", lambda *_args, **_kwargs: 0)
    monkeypatch.setattr(bcb_sgs, "set_meta", lambda *_args: None)
    monkeypatch.setattr(
        httpx,
        "get",
        lambda url, **_kwargs: httpx.Response(
            200,
            headers={"content-type": "application/json"},
            content=body,
            request=httpx.Request("GET", url),
        ),
    )
    refresh_context = set_refresh_context(run_id, "sgs_juros", run_id)
    try:
        with pytest.raises(bcb_sgs.InvalidSGSPayloadError, match=error_detail):
            bcb_sgs.fetch_sgs_series(
                433, start_date="01/01/2026", end_date="31/01/2026", use_cache=False,
                fail_fast=True,
            )
    finally:
        reset_refresh_context(refresh_context)

    rows = store.acquisition_diagnostics_for_refresh_run(run_id)["rows"]
    assert rows
    persisted = next(row for row in rows if row.get("error_detail") == error_detail)
    trace = json.loads(persisted["request_trace_json"])
    diagnostic = next(entry["diagnostics"] for entry in trace if entry["diagnostics"]["response"].get("status_code") == 200)
    assert "failure_code" not in diagnostic
    assert diagnostic["response"]["body_kind"] == body_kind
    assert diagnostic["response"]["bytes_received"] == len(body)

    with TestClient(app, raise_server_exceptions=False) as client:
        response = client.get("/api/refresh-status")
    assert response.status_code == 200
    summary = response.json()["acquisition_diagnostics"]
    item = next(value for value in summary["items"] if value["validation_reason"] == validation_reason)
    request = item["requests"][0]
    assert "failure_code" not in request
    assert request["validation_reason"] == validation_reason
    assert request["response"]["status_code"] == 200
    assert request["response"]["body_kind"] == body_kind
    assert request["response"]["bytes_received"] == len(body)
    assert request["request_context"]["series_code"] == 433
    assert request["request_context"]["requested_start"] == "2026-01-01"


def test_new_completed_claim_cannot_inherit_previous_generation_timeout(
    isolated_database: None,
) -> None:
    import json

    from db import store
    from db.store import finish_source_extraction, start_source_extraction

    run_id = "diagnostic-generation-check"
    _persist_refresh_run(run_id=run_id, manifest_version="daily-refresh-v2", tasks=["sgs_juros"])
    store._execute(
        "UPDATE refresh_work_items SET attempt = 2, claim_generation = 2 "
        "WHERE run_id = ? AND item_key = ?",
        (run_id, "sgs_juros"),
    )
    for number, generation, terminal, timestamp in (
        (1, 1, "failed", "2026-10-28T00:00:00+00:00"),
        (2, 2, "succeeded", "2026-10-29T00:00:00+00:00"),
    ):
        store._execute(
            "INSERT INTO refresh_attempt_lifecycle "
            "(attempt_id, run_id, item_key, attempt_number, claim_generation, claim_token, "
            "claim_expires_at, started_at, last_stage, terminal_outcome, terminal_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                f"attempt-{number}", run_id, "sgs_juros", number, generation,
                f"test-claim-{number}", "2026-10-30T00:00:00+00:00", timestamp,
                "finished", terminal, timestamp,
            ),
        )
    old_extraction = "old-generation-failed"
    start_source_extraction({
        "extraction_id": old_extraction, "source": "bcb_sgs", "indicator": "433",
        "qualifiers_json": "{}", "status": "running", "network_called": 1,
        "started_at": "2026-10-28T00:00:00+00:00", "run_id": run_id,
        "task_key": "sgs_juros", "cycle_id": run_id,
        "attempt_id": "attempt-1", "claim_generation": 1,
    })
    finish_source_extraction({
        "extraction_id": old_extraction, "status": "network_error", "network_called": 1,
        "finished_at": "2026-10-28T00:00:01+00:00", "error_code": "ReadTimeout",
        "error_category": "network_timeout",
        "request_trace_json": json.dumps([{
            "diagnostics": {
                "version": "source-request-diagnostics-v1", "phase": "request",
                "failure_code": "read_timeout",
                "response": {"status_code": None, "content_type": "unknown", "body_kind": "unknown"},
            },
        }]),
    })
    current_extraction = "current-generation-cache-success"
    start_source_extraction({
        "extraction_id": current_extraction, "source": "bcb_sgs", "indicator": "433",
        "qualifiers_json": "{}", "status": "running", "network_called": 0,
        "started_at": "2026-10-29T00:00:02+00:00", "run_id": run_id,
        "task_key": "sgs_juros", "cycle_id": run_id,
        "attempt_id": "attempt-2", "claim_generation": 2,
    })
    finish_source_extraction({
        "extraction_id": current_extraction, "status": "success", "network_called": 0,
        "finished_at": "2026-10-29T00:00:03+00:00", "error_code": None,
        "error_category": None, "request_trace_json": None,
    })

    payload = __import__("refresh_reporting").refresh_status_payload()
    summary = payload["acquisition_diagnostics"]
    assert summary["run_id"] == run_id
    assert not any(
        request.get("failure_code") == "read_timeout"
        for item in summary["items"]
        for request in item["requests"]
    )
    rows = store.acquisition_diagnostics_for_refresh_run(run_id)["rows"]
    assert rows[0]["extraction_id"] == current_extraction
    assert rows[0]["attempt_relation"] == "current"
    assert rows[1]["attempt_relation"] == "previous"


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

    assert payload["overall_status"] == "ok", [
        (task["task"], task["status"], task.get("data_status"), task.get("freshness"))
        for task in payload["tasks"] if task["status"] != "ok"
    ]
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


def test_v6_refresh_status_projects_frozen_recent_and_verified_archive_without_io(
    isolated_database: None, monkeypatch: pytest.MonkeyPatch,
) -> None:
    from types import MappingProxyType

    import db.sgs_refresh_plan as sgs_plan_module
    import provider_http
    from datafetchers import bcb_sgs
    from db import store
    from db.sgs_refresh_plan import (
        SGSUnitCommitReceipt,
        _coverage_from_unit_commit,
        build_sgs_plan,
        canonical_sgs_values,
        freeze_sgs_plan,
        plan_digest,
        plan_item_task_name,
        record_verified_coverage,
    )
    from main import app

    as_of = date(2026, 10, 27)
    captured_at = datetime.now(timezone.utc)
    plan = build_sgs_plan(as_of)
    run_id = "status-v6-complete"
    tasks = [plan_item_task_name(unit) for unit in plan.units]
    _persist_refresh_run(
        run_id=run_id, manifest_version="daily-refresh-v6", tasks=tasks,
        started_at=datetime(2026, 10, 27, 7, tzinfo=timezone.utc),
    )
    freeze_sgs_plan(run_id, plan, captured_at)
    for index, unit in enumerate(plan.units):
        start_text = unit.start_date.strftime("%d/%m/%Y")
        if unit.series_code == 226:
            period_end = unit.end_date + timedelta(days=1)
            records = [{"data": start_text, "dataFim": period_end.strftime("%d/%m/%Y"), "valor": "0.0032"}]
            value_hash, count, reference_start, reference_end = canonical_sgs_values(226, records)
        else:
            records = [{"data": start_text, "valor": str(index + 1)}]
            value_hash, count, reference_start, reference_end = canonical_sgs_values(unit.series_code, records)
        receipt = SGSUnitCommitReceipt(
            operation_id=f"status-v6-operation-{index}",
            extraction_id=f"status-v6-operation-{index}", run_id=run_id,
            unit_id=unit.unit_id, plan_sha256=plan_digest(plan),
            series_code=unit.series_code, contract_id=unit.contract_id,
            contract_version=unit.contract_version, identity_kind=unit.identity_kind,
            scope_role=unit.role, policy_version=unit.policy_version,
            requested_start=unit.start_date, requested_end=unit.end_date,
            payload_sha256=f"{index + 1:064x}", canonical_value_sha256=value_hash,
            observed_count=count, reference_start=reference_start, reference_end=reference_end,
            captured_at=captured_at,
        )
        if unit.series_code == 226:
            store.upsert_tr_periods(records, first_seen_at=captured_at.isoformat(),
                                    sgs_unit_commit_receipt=receipt)
        else:
            store.upsert_sgs(unit.series_code, records, first_seen_at=captured_at.isoformat(),
                             sgs_unit_commit_receipt=receipt)
        if unit.role != "recent":
            record_verified_coverage(_coverage_from_unit_commit(receipt))

    # A resumed run must retain the catalog snapshot frozen with the plan.
    monkeypatch.setattr(sgs_plan_module, "INDICATOR_CATALOG", MappingProxyType({}))

    query_count = 0
    original_query = store._query

    def counted_query(sql: str, params=()):
        nonlocal query_count
        query_count += 1
        return original_query(sql, params)

    def forbidden_io(*_args, **_kwargs):
        raise AssertionError("refresh-status GET must not perform writes or provider HTTP")

    monkeypatch.setattr(store, "_query", counted_query)
    monkeypatch.setattr(store, "_execute", forbidden_io)
    monkeypatch.setattr(store, "execute_transaction", forbidden_io)
    monkeypatch.setattr(store, "execute_statement", forbidden_io)
    monkeypatch.setattr(provider_http, "get_provider_response", forbidden_io)
    monkeypatch.setattr(bcb_sgs, "_fetch_sgs_json", forbidden_io)
    with TestClient(app) as client:
        response = client.get("/api/refresh-status")

    assert response.status_code == 200
    payload = response.json()
    summary = payload["sgs_incremental"]
    assert summary["plan_status"] == "valid"
    assert summary["recent"]["expected_units"] == sum(unit.role == "recent" for unit in plan.units)
    assert summary["recent"]["network_refreshed_units"] == sum(
        unit.role == "recent" for unit in plan.units
    )
    assert summary["archive"]["selected_units"] == sum(unit.role != "recent" for unit in plan.units)
    assert summary["archive"]["verified_units"] == summary["archive"]["selected_units"]
    assert payload["overall_status"] == "partial", [
        (task["task"], task["status"], task.get("data_status"), task.get("freshness"))
        for task in payload["tasks"] if task["status"] != "ok"
    ]
    tasks_by_name = {task["task"]: task for task in payload["tasks"]}
    assert tasks_by_name["sgs_juros"]["data_status"] == "ok"
    assert tasks_by_name["sgs_juros"]["acquisition_status"] == "network_refreshed"
    assert tasks_by_name["sgs_juros"]["last_data_captured_at"] == captured_at.isoformat()
    assert tasks_by_name["sgs_juros"]["series_reference_freshness_details"]
    assert all(
        detail["policy_version"] == "reference-freshness-v2"
        for detail in tasks_by_name["sgs_juros"]["series_reference_freshness_details"].values()
    )
    assert not any(task["task"].startswith("sgs_juros_w") for task in payload["tasks"])
    assert query_count < 15


def _seed_v6_status_run(run_id: str = "status-v6-matrix") -> Any:
    from db.sgs_refresh_plan import build_sgs_plan, freeze_sgs_plan, plan_item_task_name

    plan = build_sgs_plan(date(2026, 10, 27))
    _persist_refresh_run(
        run_id=run_id, manifest_version="daily-refresh-v6",
        tasks=[plan_item_task_name(unit) for unit in plan.units],
        started_at=datetime(2026, 10, 27, 7, tzinfo=timezone.utc),
    )
    freeze_sgs_plan(run_id, plan, datetime(2026, 10, 27, 8, tzinfo=timezone.utc))
    return plan


def test_v6_status_reads_unchanged_frozen_v1_plan_and_supported_receipt_inventory(
    isolated_database: None,
) -> None:
    from dataclasses import replace

    from db import store
    from db.sgs_refresh_plan import (
        build_sgs_plan,
        freeze_sgs_plan,
        plan_digest,
        plan_item_task_name,
    )
    from main import app

    current = build_sgs_plan(date(2026, 10, 27), max_archive_units=0)
    old = replace(
        current,
        policy_version="sgs-coverage-policy-v1",
        units=tuple(replace(unit, policy_version="sgs-coverage-policy-v1") for unit in current.units),
    )
    run_id = "status-v6-frozen-v1-policy"
    _persist_refresh_run(
        run_id=run_id, manifest_version="daily-refresh-v6",
        tasks=[plan_item_task_name(unit) for unit in old.units],
        started_at=datetime(2026, 10, 27, 7, tzinfo=timezone.utc),
    )
    frozen = freeze_sgs_plan(run_id, old, datetime(2026, 10, 27, 8, tzinfo=timezone.utc))
    stored = store.query_rows(
        "SELECT plan_json, plan_sha256 FROM sgs_refresh_plans WHERE run_id = ?", (run_id,),
    )[0]
    assert frozen == old
    assert stored["plan_sha256"] == plan_digest(old)
    with TestClient(app) as client:
        response = client.get("/api/refresh-status")
    assert response.status_code == 200
    assert response.json()["sgs_incremental"]["plan_status"] == "valid"
    legacy_availability = response.json()["sgs_incremental"]["archive"]["availability"]
    assert legacy_availability["status"] == "not_recorded"
    assert legacy_availability["by_series"] == []


def test_refresh_status_accepts_old64_v1_plan_with_current68_catalog_readonly(
    isolated_database: None, monkeypatch: pytest.MonkeyPatch,
) -> None:
    from types import MappingProxyType

    import db.sgs_refresh_plan as plan_module
    from db import store
    from db.sgs_availability import availability_for_version
    from db.sgs_refresh_plan import build_sgs_plan, freeze_sgs_plan, plan_item_task_name
    from main import app

    new_codes = {20573, 20574, 20587, 20588}
    old_catalog = {key: spec for key, spec in plan_module.INDICATOR_CATALOG.items()
                   if int(spec.provider_code) not in new_codes}
    monkeypatch.setattr(plan_module, "INDICATOR_CATALOG", MappingProxyType(old_catalog))
    monkeypatch.setattr(plan_module, "availability_for", lambda code, frequency=None:
                        availability_for_version("bcb-sgs-start-periods-v1", code, frequency))
    plan = build_sgs_plan(date(2026, 10, 28))
    assert sum(unit.role == "recent" for unit in plan.units) == 64
    run_id = "status-old64-v1-current68"
    _persist_refresh_run(
        run_id=run_id, manifest_version="daily-refresh-v6",
        tasks=[plan_item_task_name(unit) for unit in plan.units],
        started_at=datetime(2026, 10, 28, 7, tzinfo=timezone.utc),
    )
    freeze_sgs_plan(run_id, plan, datetime(2026, 10, 28, 8, tzinfo=timezone.utc))
    stored_before = store.query_rows(
        "SELECT plan_json, plan_sha256 FROM sgs_refresh_plans WHERE run_id = ?", (run_id,),
    )[0]
    monkeypatch.undo()

    def forbidden_write(*_args, **_kwargs):
        raise AssertionError("refresh-status must remain read-only")

    monkeypatch.setattr(store, "_execute", forbidden_write)
    monkeypatch.setattr(store, "execute_transaction", forbidden_write)
    monkeypatch.setattr(store, "execute_statement", forbidden_write)
    with TestClient(app) as client:
        first = client.get("/api/refresh-status")
        second = client.get("/api/refresh-status")
    assert first.status_code == second.status_code == 200
    summary = first.json()["sgs_incremental"]
    assert summary["plan_status"] == "valid"
    assert summary["recent"]["expected_units"] == 64
    assert not any(code in {20573, 20574, 20587, 20588}
                   for code in summary["recent"].get("by_series", []))
    assert store.query_rows(
        "SELECT plan_json, plan_sha256 FROM sgs_refresh_plans WHERE run_id = ?", (run_id,),
    )[0] == stored_before


@pytest.mark.parametrize(
    ("corruption", "expected_error"),
    [
        ("missing_unit", "IncompleteFrozenSGSPlanUnits"),
        ("changed_membership", "FrozenSGSPlanUnitMismatch"),
        ("bad_hash", "InvalidFrozenSGSPlan"),
        ("bad_version", "InvalidFrozenSGSPlan"),
        ("boolean_freshness", "InvalidFrozenSGSPlan"),
        ("wrong_identity", "InvalidFrozenSGSPlanUnitMetadata"),
    ],
)
def test_v6_invalid_frozen_plan_degrades_as_json_without_false_green(
    isolated_database: None, corruption: str, expected_error: str,
) -> None:
    from db import store
    from main import app

    run_id = f"status-v6-{corruption}"
    plan = _seed_v6_status_run(run_id)
    if corruption == "missing_unit":
        store._execute(
            "DELETE FROM sgs_refresh_plan_units WHERE run_id = ? AND unit_id = ?",
            (run_id, plan.units[0].unit_id),
        )
    elif corruption == "changed_membership":
        store._execute(
            "UPDATE sgs_refresh_plan_units SET group_name = ? WHERE run_id = ? AND unit_id = ?",
            ("INVENTED", run_id, plan.units[0].unit_id),
        )
    else:
        if corruption == "bad_hash":
            store._execute(
                "UPDATE sgs_refresh_plans SET plan_sha256 = ? WHERE run_id = ?",
                ("0" * 64, run_id),
            )
        elif corruption == "bad_version":
            store._execute(
                "UPDATE sgs_refresh_plans SET plan_version = ? WHERE run_id = ?",
                ("unknown-plan-v99", run_id),
            )
        else:
            row = store.query_rows(
                "SELECT plan_json FROM sgs_refresh_plans WHERE run_id = ?", (run_id,)
            )[0]
            body = json.loads(row["plan_json"])
            if corruption == "boolean_freshness":
                body["units"][0]["freshness_days"] = True
            else:
                body["units"][0]["identity_kind"] = "wrong_identity"
                store._execute(
                    "UPDATE sgs_refresh_plan_units SET identity_kind = ? WHERE run_id = ? AND unit_id = ?",
                    ("wrong_identity", run_id, plan.units[0].unit_id),
                )
            encoded = json.dumps(body, sort_keys=True, separators=(",", ":"))
            store._execute(
                "UPDATE sgs_refresh_plans SET plan_json = ?, plan_sha256 = ? WHERE run_id = ?",
                (encoded, hashlib.sha256(encoded.encode()).hexdigest(), run_id),
            )

    with TestClient(app, raise_server_exceptions=False) as client:
        response = client.get("/api/refresh-status")
    assert response.status_code == 200
    payload = response.json()
    assert payload["overall_status"] == "partial"
    assert payload["sgs_incremental"]["plan_status"] == "degraded"
    assert payload["sgs_incremental"]["plan_error"] == expected_error
    assert payload["tasks"][0]["status"] == "partial"


@pytest.mark.parametrize("state", ["missing_capture", "cached_without_receipt", "stale_capture", "deferred_recent"])
def test_v6_current_recent_unresolved_states_remain_partial_and_cache_is_not_capture(
    isolated_database: None, state: str,
) -> None:
    from db import store
    from db.sgs_refresh_plan import (
        SGSUnitCommitReceipt,
        canonical_sgs_values,
        plan_digest,
    )
    from main import app

    run_id = f"status-v6-recent-{state}"
    plan = _seed_v6_status_run(run_id)
    recent = next(unit for unit in plan.units if unit.role == "recent")
    if recent.series_code == 226:
        period_end = recent.end_date + timedelta(days=1)
        records = [{"data": recent.start_date.strftime("%d/%m/%Y"),
                    "dataFim": period_end.strftime("%d/%m/%Y"), "valor": "0.0032"}]
        value_hash, count, ref_start, ref_end = canonical_sgs_values(226, records)
        if state != "missing_capture":
            store.upsert_tr_periods(records, first_seen_at="2025-01-01T00:00:00+00:00")
    else:
        records = [{"data": recent.start_date.strftime("%d/%m/%Y"), "valor": "5"}]
        value_hash, count, ref_start, ref_end = canonical_sgs_values(recent.series_code, records)
        if state != "missing_capture":
            store.upsert_sgs(recent.series_code, records, first_seen_at="2025-01-01T00:00:00+00:00")
    assert count > 0
    if state == "stale_capture":
        receipt = SGSUnitCommitReceipt(
            operation_id="status-old-capture", extraction_id="status-old-capture",
            run_id=run_id, unit_id=recent.unit_id, plan_sha256=plan_digest(plan),
            series_code=recent.series_code, contract_id=recent.contract_id,
            contract_version=recent.contract_version, identity_kind=recent.identity_kind,
            scope_role=recent.role, policy_version=recent.policy_version,
            requested_start=recent.start_date, requested_end=recent.end_date,
            payload_sha256="a" * 64, canonical_value_sha256=value_hash,
            observed_count=count, reference_start=ref_start, reference_end=ref_end,
            captured_at=datetime(2025, 1, 1, tzinfo=timezone.utc),
        )
        if recent.series_code == 226:
            store.upsert_tr_periods(records, first_seen_at=receipt.captured_at.isoformat(),
                                    sgs_unit_commit_receipt=receipt)
        else:
            store.upsert_sgs(recent.series_code, records,
                             first_seen_at=receipt.captured_at.isoformat(),
                             sgs_unit_commit_receipt=receipt)
    if state == "deferred_recent":
        task_key = f"sgs_plan_{recent.unit_id.replace(':', '_')}"
        store._execute(
            "UPDATE refresh_work_items SET status = 'pending', deferral_reason = ?, "
            "next_attempt_at = ? WHERE run_id = ? AND item_key = ?",
            ("quota", "2026-10-28T00:00:00+00:00", run_id, task_key),
        )

    with TestClient(app, raise_server_exceptions=False) as client:
        response = client.get("/api/refresh-status")
    assert response.status_code == 200
    payload = response.json()
    assert payload["overall_status"] == "partial"
    group = next(
        task for task in payload["tasks"]
        if task["task"] == "sgs_atividade" and recent.group_name in {"ACTIVITY", "ACTIVITY_SECTORAL"}
    ) if recent.group_name in {"ACTIVITY", "ACTIVITY_SECTORAL"} else next(
        task for task in payload["tasks"]
        if task["task"] == {
            "EXCHANGE": "sgs_cambio", "INTEREST_RATES": "sgs_juros",
            "INFLATION": "sgs_inflacao", "FISCAL": "sgs_fiscal", "CREDIT": "sgs_credito",
            "EXTERNAL": "sgs_setor_externo", "COMPLEMENTARY": "sgs_complementares",
            "IPCA_GROUPS": "sgs_ipca_grupos", "IPCA_NATURE": "sgs_ipca_naturezas",
            "IPCA_CORE": "sgs_ipca_nucleos", "IPCA_PRICES": "sgs_ipca_precos",
        }[recent.group_name]
    )
    assert group["status"] == "partial"
    if state in {"missing_capture", "cached_without_receipt"}:
        assert group["last_data_captured_at"] is None
    if state == "missing_capture":
        assert group["data_status"] == "unavailable"
        assert group["series_network_refreshed"] == 0
    if state == "cached_without_receipt":
        assert group["data_status"] == "unverified"
        assert group["series_available"] >= 1
        assert group["series_network_refreshed"] == 0
    if state == "stale_capture":
        assert group["last_data_captured_at"].startswith("2025-01-01")
        assert group["data_status"] == "stale"
        assert group["series_network_refreshed"] >= 1
    if state == "deferred_recent":
        assert group["acquisition_status"] == "deferred"
        assert group["provider_deferred"] == 1


def _commit_v6_interval_unit(run_id: str, unit, records: list[dict[str, str]], captured_at: datetime) -> None:
    from db import store
    from db.sgs_refresh_plan import (
        SGSUnitCommitReceipt,
        canonical_sgs_values,
        plan_digest,
        read_sgs_plan,
    )

    plan = read_sgs_plan(run_id)
    assert plan is not None
    payload_hash, count, reference_start, reference_end = canonical_sgs_values(226, records)
    receipt = SGSUnitCommitReceipt(
        operation_id=f"{run_id}-tr-commit", extraction_id=f"{run_id}-tr-commit",
        run_id=run_id, unit_id=unit.unit_id, plan_sha256=plan_digest(plan),
        series_code=226, contract_id=unit.contract_id, contract_version=unit.contract_version,
        identity_kind=unit.identity_kind, scope_role=unit.role, policy_version=unit.policy_version,
        requested_start=unit.start_date, requested_end=unit.end_date,
        payload_sha256="c" * 64, canonical_value_sha256=payload_hash,
        observed_count=count, reference_start=reference_start, reference_end=reference_end,
        captured_at=captured_at,
    )
    store.upsert_tr_periods(records, first_seen_at=captured_at.isoformat(),
                            sgs_unit_commit_receipt=receipt)


def test_v6_tr_status_projects_latest_eligible_start_and_separate_future_period_end(
    isolated_database: None,
) -> None:
    from db import store
    from main import app

    run_id = "status-v6-tr-reference-max"
    plan = _seed_v6_status_run(run_id)
    tr = next(unit for unit in plan.units if unit.series_code == 226 and unit.role == "recent")
    captured = datetime(2026, 10, 27, 8, tzinfo=timezone.utc)
    def record(start: str, end: str, value: str) -> dict[str, str]:
        return {"data": start, "dataFim": end, "valor": value}

    rows = [
        record("29/07/2026", "30/07/2026", "0.1"),
        record("26/09/2026", "27/09/2026", "0.2"),
        record("26/10/2026", "25/11/2026", "0.3"),
        record("27/10/2026", "26/11/2026", "0.4"),
    ]
    # This persisted future-start interval is outside the frozen request and must not project.
    store.upsert_tr_periods(
        [record("28/10/2026", "27/11/2026", "0.5")], first_seen_at=captured.isoformat(),
    )
    _commit_v6_interval_unit(run_id, tr, rows, captured)

    with TestClient(app) as client:
        response = client.get("/api/refresh-status")
    assert response.status_code == 200
    payload = response.json()
    juros = next(task for task in payload["tasks"] if task["task"] == "sgs_juros")
    assert juros["current_reference_max"] == "2026-10-27"
    assert juros["series_reference_freshness"]["226"] == "ok"
    assert juros["series_period_end_max"] == {"226": "2026-11-26"}
    inventory = store.sgs_plan_status_inventory(run_id)
    interval = next(row for row in inventory if row["series_code"] == 226 and row["role"] == "recent")
    assert interval["reference_start"] == "2026-07-29"
    assert interval["reference_end"] == "2026-11-26"
    assert interval["eligible_reference_max"] == "2026-10-27"
    assert interval["eligible_period_end_max"] == "2026-11-26"
    availability = payload["sgs_incremental"]["archive"]["availability"]
    assert availability["status"] == "documented_boundary_only"
    assert availability["registry_version"] == "bcb-sgs-start-periods-v2"
    assert len(availability["registry_sha256"]) == 64
    assert len(availability["by_series"]) == 68
    inpc = next(item for item in availability["by_series"] if item["series_code"] == 1635)
    assert (inpc["documented_start"], inpc["first_reference_period"]) == (
        "1991-01-01", "1991-01-01",
    )
    excluded = next(item for item in availability["excluded_scopes"] if item["series_code"] == 1635)
    assert (excluded["start_date"], excluded["end_date"], excluded["reason"]) == (
        "1990-01-01", "1990-12-31", "before_documented_start",
    )
    assert availability["basis"] == "documented_start_not_coverage_proof"
    facts = {item["series_code"]: item for item in availability["by_series"]}
    assert facts[1639]["first_reference_period"] == "1999-08-01"
    assert facts[4466]["first_reference_period"] == "1991-12-01"
    assert facts[13762]["first_reference_period"] == "2006-12-01"
    assert facts[25353]["first_reference_period"] == "2013-01-01"
    assert facts[1207]["first_reference_period"] == "1962-01-01"
    assert facts[1207]["approved_history_start"] == "2015-01-01"
    assert payload["sgs_incremental"]["archive"]["selected_units"] == len(
        [unit for unit in plan.units if unit.role != "recent"]
    )


def test_v6_tr_old_start_with_future_end_is_stale_and_date_only_cache_unverified(
    isolated_database: None,
) -> None:
    from main import app

    run_id = "status-v6-tr-stale-start"
    plan = _seed_v6_status_run(run_id)
    tr = next(unit for unit in plan.units if unit.series_code == 226 and unit.role == "recent")
    captured = datetime(2026, 10, 27, 8, tzinfo=timezone.utc)
    old = {"data": "29/07/2026", "dataFim": "01/12/2026", "valor": "0.1"}
    _commit_v6_interval_unit(run_id, tr, [old], captured)
    with TestClient(app) as client:
        response = client.get("/api/refresh-status")
    juros = next(task for task in response.json()["tasks"] if task["task"] == "sgs_juros")
    assert juros["current_reference_max"] == "2026-07-29"
    assert juros["series_reference_freshness"]["226"] == "stale"
    assert juros["series_period_end_max"] == {"226": "2026-12-01"}


def test_v6_tr_legacy_date_only_rows_do_not_attest_interval_reference(isolated_database: None) -> None:
    from db import store
    from main import app

    legacy_run = "status-v6-tr-legacy-only"
    _seed_v6_status_run(legacy_run)
    captured = datetime(2026, 10, 27, 8, tzinfo=timezone.utc)
    old = {"data": "29/07/2026", "dataFim": "01/12/2026", "valor": "0.1"}
    store.upsert_tr_periods([old], first_seen_at=captured.isoformat())
    with TestClient(app) as client:
        legacy_response = client.get("/api/refresh-status")
    legacy_juros = next(task for task in legacy_response.json()["tasks"] if task["task"] == "sgs_juros")
    assert legacy_juros["current_reference_max"] is None
    assert legacy_juros["series_reference_freshness"]["226"] == "unverified"
    assert legacy_juros["series_period_end_max"] == {}


def test_v6_status_reports_proven_preavailability_conflict_without_changing_scope(
    isolated_database: None,
) -> None:
    from dataclasses import replace

    from db import store
    from db.sgs_refresh_plan import (
        CoverageReceipt,
        SGSUnitCommitReceipt,
        canonical_sgs_values,
        freeze_sgs_plan,
        plan_digest,
        record_verified_coverage,
    )
    from main import app

    run_id = "status-v6-availability-conflict"
    new_plan = _seed_v6_status_run(run_id)
    current = next(unit for unit in new_plan.units if unit.series_code == 1635 and unit.role == "recent")
    historical = replace(
        current, unit_id="1635:archive_backfill:1990-01-01:1990-12-31",
        ordinal=len(new_plan.units), role="archive_backfill",
        policy_version="sgs-coverage-policy-v2", start_date=date(1990, 1, 1),
        end_date=date(1990, 12, 31), availability_start=None,
        availability_documented_start=None, availability_registry_version=None,
        availability_observed_on=None, availability_basis=None, availability_source_url=None,
        availability_detail_url=None, availability_registry_sha256=None,
        approved_history_start=None,
    )
    old_units = tuple(replace(
        unit, policy_version="sgs-coverage-policy-v2", availability_start=None,
        availability_documented_start=None, availability_registry_version=None,
        availability_observed_on=None, availability_basis=None, availability_source_url=None,
        availability_detail_url=None, availability_registry_sha256=None,
        approved_history_start=None,
    ) for unit in new_plan.units) + (historical,)
    old_plan = replace(new_plan, policy_version="sgs-coverage-policy-v2", units=old_units)
    freeze_sgs_plan("status-v6-preavailability-proof-old", old_plan)
    old_unit = old_plan.units[-1]
    rows = [{"data": "01/01/1990", "valor": "1.25"}]
    value_hash, count, reference_start, reference_end = canonical_sgs_values(1635, rows)
    captured = datetime(2025, 1, 2, tzinfo=timezone.utc)
    commit = SGSUnitCommitReceipt(
        operation_id="status-v6-preavailability-proof", extraction_id="status-v6-preavailability-proof",
        run_id="status-v6-preavailability-proof-old", unit_id=old_unit.unit_id,
        plan_sha256=plan_digest(old_plan), series_code=1635, contract_id=old_unit.contract_id,
        contract_version=old_unit.contract_version, identity_kind=old_unit.identity_kind,
        scope_role=old_unit.role, policy_version=old_unit.policy_version,
        requested_start=old_unit.start_date, requested_end=old_unit.end_date,
        payload_sha256="e" * 64, canonical_value_sha256=value_hash,
        observed_count=count, reference_start=reference_start, reference_end=reference_end,
        captured_at=captured,
    )
    store.upsert_sgs(1635, rows, sgs_unit_commit_receipt=commit)
    assert record_verified_coverage(CoverageReceipt(
        operation_id=commit.operation_id, run_id=commit.run_id, unit_id=commit.unit_id,
        source="bcb_sgs", series_code=1635, contract_id=commit.contract_id,
        contract_version=commit.contract_version, identity_kind=commit.identity_kind,
        scope_role=commit.scope_role, policy_version=commit.policy_version,
        requested_start=commit.requested_start, requested_end=commit.requested_end,
        payload_sha256=commit.payload_sha256, observed_count=commit.observed_count,
        captured_at=commit.captured_at, reference_start=commit.reference_start,
        reference_end=commit.reference_end, business_receipt_id=commit.extraction_id,
    ))

    with TestClient(app) as client:
        response = client.get("/api/refresh-status")

    assert response.status_code == 200
    projection = response.json()["sgs_incremental"]["archive"]["availability"]
    assert projection["status"] == "conflict_requires_review"
    assert projection["conflicts"] == [{
        "series_code": 1635, "earliest_proven_reference": "1990-01-01",
        "documented_first_reference_period": "1991-01-01",
        "disposition": "conservative_unknown_metadata_review",
    }]
    assert response.json()["sgs_incremental"]["archive"]["selected_units"] == 4
    assert store.query_rows(
        "SELECT reference_start FROM sgs_coverage_receipts WHERE operation_id = ?",
        (commit.operation_id,),
    )[0]["reference_start"] == "1990-01-01"


@pytest.mark.parametrize("archive_state", ["failed", "deferred"])
def test_v6_selected_archive_failure_is_attention_but_unselected_rotation_is_informational(
    isolated_database: None, archive_state: str,
) -> None:
    from db import store
    from main import app

    run_id = "status-v6-archive-failure"
    plan = _seed_v6_status_run(run_id)
    archive = next(unit for unit in plan.units if unit.role != "recent")
    task_key = f"sgs_plan_{archive.unit_id.replace(':', '_')}"
    if archive_state == "failed":
        store._execute(
            "UPDATE refresh_work_items SET status = 'failed', error_code = ? "
            "WHERE run_id = ? AND item_key = ?",
            ("ArchiveTimeout", run_id, task_key),
        )
    else:
        store._execute(
            "UPDATE refresh_work_items SET status = 'pending', deferral_reason = ?, "
            "next_attempt_at = ? WHERE run_id = ? AND item_key = ?",
            ("quota", "2026-10-28T00:00:00+00:00", run_id, task_key),
        )
    with TestClient(app, raise_server_exceptions=False) as client:
        response = client.get("/api/refresh-status")
    payload = response.json()
    summary = payload["sgs_incremental"]["archive"]
    assert response.status_code == 200
    assert payload["overall_status"] == "partial"
    assert summary["failed_units"] == (1 if archive_state == "failed" else 0)
    assert summary["deferred_units"] == (1 if archive_state == "deferred" else 0)
    assert summary["backlog_status"] == "selected_scope_attention"
    assert summary["unselected_backlog"] == "informational_bounded_rotation"


def test_v6_recent_success_does_not_erase_historical_v5_archive_failure(
    isolated_database: None,
) -> None:
    from db import store
    from main import app

    run_id = "status-v6-retained-history"
    _seed_v6_status_run(run_id)
    legacy_task = "sgs_juros_w40502861447_2015-01-01_2015-12-31"
    store.record_refresh_task_start(
        legacy_task, "bcb_sgs", 1, "2025-01-01T00:00:00+00:00"
    )
    store.record_refresh_task_finish(
        legacy_task, "error", 0, 0, 10, "ArchiveMismatch",
        "2025-01-01T00:01:00+00:00",
    )
    before = store.query_rows(
        "SELECT status, error FROM refresh_task_status WHERE task = ?", (legacy_task,)
    )[0]
    with TestClient(app, raise_server_exceptions=False) as client:
        response = client.get("/api/refresh-status")
    after = store.query_rows(
        "SELECT status, error FROM refresh_task_status WHERE task = ?", (legacy_task,)
    )[0]
    assert response.status_code == 200
    payload = response.json()
    assert payload["overall_status"] == "partial"
    assert not any(task["task"] == legacy_task for task in payload["tasks"])
    assert {entry["task"] for entry in payload["sgs_incremental"]["legacy_sgs_history"]} == {
        legacy_task
    }
    assert before == after == {"status": "error", "error": "ArchiveMismatch"}


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


def test_refresh_status_projects_current_v5_ipca_children_over_stale_legacy_error(
    isolated_database: None,
) -> None:
    from db.refresh_runs import read_latest_run
    from main import app

    tasks = [*_ipca_window_tasks(), *(f"other_task_{index}" for index in range(21))]
    _persist_refresh_run(
        run_id="refresh-v5-current-ipca",
        manifest_version="daily-refresh-v5",
        tasks=tasks,
    )
    with TestClient(app) as client:
        response = client.get("/api/refresh-status")

    group = next(task for task in response.json()["tasks"] if task["task"] == "sgs_ipca_grupos")
    snapshot = read_latest_run()
    assert response.status_code == 200
    assert snapshot is not None and snapshot.run.manifest_version == "daily-refresh-v5"
    assert group["status"] == "ok"
    assert group["series_updated"] == group["series_expected"] == 9
    assert group["error_code"] is None


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
