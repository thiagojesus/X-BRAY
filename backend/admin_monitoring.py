"""Bounded, read-only projections for authenticated human administration."""

from __future__ import annotations

import base64
import json
import re
import sqlite3
from collections.abc import Mapping, Sequence
from datetime import date, datetime, timedelta, timezone
from enum import StrEnum
from typing import Annotated, Any, cast

import psycopg
from fastapi import APIRouter, Depends, HTTPException, Query, Request
from pydantic import BaseModel, ConfigDict

from acquisition_diagnostics import (
    sanitize_public_summary,
    summarize_refresh_run_diagnostics,
)
from admin_auth import require_admin
from db import store
from db.refresh_run_sql import execute, read_transaction
from db.sgs_refresh_plan import SUPPORTED_POLICY_VERSIONS
from indicator_catalog import INDICATOR_CATALOG, IndicatorSpec

router = APIRouter(prefix="/api/admin", tags=["admin-monitoring"], dependencies=[Depends(require_admin)])
DEFAULT_LIMIT = 50
MAX_LIMIT = 100
DEFAULT_DAYS = 30
MAX_CURSOR = 2048
MAX_FINAL_JSON_BYTES = 65536
COVERAGE_CANDIDATE_BUDGET = 128
_COVERAGE_CANDIDATE_LIMIT = COVERAGE_CANDIDATE_BUDGET + 1
_RUN_ID = re.compile(r"[A-Za-z0-9_.:-]{1,128}\Z")
_CATEGORIES = frozenset({
    "network_timeout", "network_reset", "temporary_server_error", "rate_limited",
    "envelope_error", "invalid_schema", "semantic_conflict", "incomplete_coverage",
    "persistence_failure", "freshness_failure", "logging_failure", "configuration_error",
    "authentication_error", "forbidden_host", "internal_error", "deferred", "circuit_open",
})
_SAFE_ERROR_CODES = frozenset({
    "ReadTimeout", "ConnectTimeout", "WriteTimeout", "PoolTimeout", "TimeoutError",
    "HTTPStatusError", "InvalidSGSPayloadError", "ProviderInvalidJSONResponseError",
    "ProviderResponseDecodingError", "NoSuccessfulSourceTasks", "retired_source",
    "invalid_sgs_payload", "invalid_json", "content_decoding_error", "network_timeout",
    "network_reset", "temporary_server_error", "rate_limited", "envelope_error",
    "invalid_schema", "semantic_conflict", "incomplete_coverage", "persistence_failure",
    "freshness_failure", "logging_failure", "configuration_error", "authentication_error",
    "forbidden_host", "internal_error", "deferred", "circuit_open",
})
_PROVIDERS = ("bcb_sgs", "focus", "tesouro", "anbima", "b3", "ibge_sidra")
_PROVIDER_CAPTURE_SQL = (
    "SELECT provider, MAX(captured_at) AS captured_at FROM ("
    "SELECT e.source AS provider, c.captured_at AS captured_at FROM sgs_unit_commits c "
    "JOIN sgs_refresh_plans p ON p.run_id = c.run_id AND p.plan_sha256 = c.plan_sha256 "
    "JOIN sgs_refresh_plan_units u ON u.run_id = c.run_id AND u.unit_id = c.unit_id AND u.series_code = c.series_code AND u.contract_version = c.contract_version AND u.identity_kind = c.identity_kind AND u.role = c.scope_role AND u.policy_version = c.policy_version AND u.start_date = c.requested_start AND u.end_date = c.requested_end "
    "JOIN source_extractions e ON e.extraction_id = c.extraction_id AND e.run_id = c.run_id AND e.task_key = ('sgs_plan_' || replace(u.unit_id, ':', '_')) WHERE e.source = 'bcb_sgs' "
    "UNION ALL SELECT e.source, t.captured_at FROM treasury_cohort_commits t JOIN source_extractions e ON e.extraction_id = t.extraction_id "
    "UNION ALL SELECT e.source, e.network_captured_at FROM source_acquisition_commits c JOIN source_extractions e ON e.extraction_id = c.extraction_id WHERE c.selected_route = 'primary'"
    ") capture_rows GROUP BY provider"
)
_NON_SGS_CAPTURE_SQL = (
    "SELECT MAX(captured_at) AS captured_at FROM ("
    "SELECT t.captured_at FROM treasury_cohort_commits t "
    "JOIN source_extractions e ON e.extraction_id = t.extraction_id WHERE e.source = ? "
    "UNION ALL SELECT e.network_captured_at FROM source_acquisition_commits c "
    "JOIN source_extractions e ON e.extraction_id = c.extraction_id "
    "WHERE c.selected_route = 'primary' AND e.source = ?"
    ") provider_captures"
)


class RunState(StrEnum):
    RUNNING = "running"
    SUCCEEDED = "succeeded"
    PARTIAL = "partial"
    FAILED = "failed"


class WorkState(StrEnum):
    PENDING = "pending"
    CLAIMED = "claimed"
    SUCCEEDED = "succeeded"
    PARTIAL = "partial"
    FAILED = "failed"
    SKIPPED = "skipped"


class ProviderCode(StrEnum):
    BCB_SGS = "bcb_sgs"
    FOCUS = "focus"
    TESOURO = "tesouro"
    ANBIMA = "anbima"
    B3 = "b3"
    IBGE_SIDRA = "ibge_sidra"


class Envelope(BaseModel):
    model_config = ConfigDict(extra="forbid")
    generated_at: str
    as_of: str
    scope: str


class RunPage(Envelope):
    items: list[dict[str, Any]]
    limit: int
    has_more: bool
    next_cursor: str | None


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _timestamp(value: object) -> str | None:
    if isinstance(value, datetime):
        parsed = value
    elif isinstance(value, str) and len(value) <= 64:
        try:
            parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
        except ValueError:
            return None
    else:
        return None
    if parsed.tzinfo is None:
        return None
    return parsed.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")


def _db_time_expr(field: str) -> str:
    if field not in {"created_at"}:
        raise ValueError("unsupported timestamp field")
    return f"{field}::timestamptz" if store.uses_postgres() else f"julianday({field})"


def _cursor_encode(value: dict[str, object]) -> str:
    raw = json.dumps(value, sort_keys=True, separators=(",", ":")).encode()
    return base64.urlsafe_b64encode(raw).rstrip(b"=").decode("ascii")


def _cursor_decode(value: str, expected: Mapping[str, object]) -> dict[str, object]:
    if len(value) > MAX_CURSOR or re.fullmatch(r"[A-Za-z0-9_-]+", value) is None:
        raise HTTPException(422, detail={"error": "invalid_cursor"})
    try:
        raw = base64.urlsafe_b64decode(value + "=" * (-len(value) % 4))
        decoded = json.loads(raw)
    except (ValueError, json.JSONDecodeError):
        raise HTTPException(422, detail={"error": "invalid_cursor"}) from None
    decoded_map = cast(dict[str, object], decoded) if isinstance(decoded, dict) else None
    if decoded_map is None or set(decoded_map) != {*expected, "since", "until", "created_at", "run_id", "anchor"}:
        raise HTTPException(422, detail={"error": "invalid_cursor"})
    if any(decoded_map.get(key) != item for key, item in expected.items()):
        raise HTTPException(422, detail={"error": "cursor_filter_mismatch"})
    run_id = decoded_map.get("run_id")
    if not isinstance(run_id, str) or not _RUN_ID.fullmatch(run_id):
        raise HTTPException(422, detail={"error": "invalid_cursor"})
    if _timestamp(decoded_map.get("created_at")) is None:
        raise HTTPException(422, detail={"error": "invalid_cursor"})
    if _timestamp(decoded_map.get("anchor")) is None:
        raise HTTPException(422, detail={"error": "invalid_cursor"})
    if _timestamp(decoded_map.get("since")) is None or _timestamp(decoded_map.get("until")) is None:
        raise HTTPException(422, detail={"error": "invalid_cursor"})
    return decoded_map


def _row_dict(row: object) -> dict[str, object]:
    return cast(dict[str, object], dict(cast(Any, row)))


def _read(sql: str, params: tuple[object, ...] = ()) -> list[dict[str, object]]:
    with read_transaction() as connection:
        if store.uses_postgres():
            execute(connection, "SET TRANSACTION READ ONLY")
        cursor = execute(connection, sql, cast(tuple[str | int | None, ...], params))
        return [_row_dict(row) for row in cursor.fetchall()]


def _read_batch(queries: list[tuple[str, tuple[object, ...]]]) -> list[list[dict[str, object]]]:
    results: list[list[dict[str, object]]] = []
    with read_transaction() as connection:
        if store.uses_postgres():
            execute(connection, "SET TRANSACTION READ ONLY")
        for sql, params in queries:
            cursor = execute(connection, sql, cast(tuple[str | int | None, ...], params))
            results.append([_row_dict(row) for row in cursor.fetchall()])
    return results


def _safe_state(value: object, enum: type[StrEnum]) -> str:
    try:
        return enum(str(value)).value
    except ValueError:
        return "metadata_unknown"


def _int(value: object) -> int | None:
    return value if type(value) is int and value >= 0 else None


def _safe_identifier(value: object, max_length: int) -> str | None:
    return value if isinstance(value, str) and len(value) <= max_length and re.fullmatch(r"[A-Za-z0-9_.:-]+", value) else None


def _run_summary(row: dict[str, object], counts: dict[str, dict[str, int]] | None = None) -> dict[str, object]:
    created_at = _timestamp(row.get("created_at"))
    finished_at = _timestamp(row.get("finished_at"))
    updated_at = _timestamp(row.get("updated_at"))
    raw_run_id = row.get("run_id")
    raw_cycle_id = row.get("cycle_id")
    raw_manifest = row.get("manifest_version")
    safe_run_id = _safe_identifier(raw_run_id, 128)
    run_id = safe_run_id if safe_run_id is not None else "metadata_unknown"
    return {
        "run_id": run_id,
        "cycle_id": _safe_identifier(raw_cycle_id, 128),
        "state": _safe_state(row.get("state"), RunState),
        "created_at": created_at,
        "updated_at": updated_at,
        "finished_at": finished_at,
        "duration_seconds": _duration(created_at, finished_at or updated_at),
        "manifest_version": _safe_identifier(raw_manifest, 80) or "metadata_unknown",
        "force": row.get("force") in (1, True),
        "task_counts": counts or {},
        "workflow_url": f"https://github.com/thiagojesus/X-BRAY/actions/runs/{run_id}" if run_id.isdecimal() and int(run_id) > 0 else None,
    }


def _duration(start: str | None, end: str | None) -> float | None:
    if start is None or end is None:
        return None
    try:
        return max(0.0, (datetime.fromisoformat(end.replace("Z", "+00:00")) - datetime.fromisoformat(start.replace("Z", "+00:00"))).total_seconds())
    except ValueError:
        return None


def _task_counts_from_rows(rows: Sequence[dict[str, object]]) -> dict[str, dict[str, dict[str, int]]]:
    result: dict[str, dict[str, dict[str, int]]] = {}
    for row in rows:
        rid, task, status = row.get("run_id"), row.get("task"), _safe_state(row.get("status"), WorkState)
        if not isinstance(rid, str) or not isinstance(task, str):
            continue
        task_counts = result.setdefault(rid, {}).setdefault(task[:128], {})
        count = _int(row.get("total"))
        if count is not None:
            task_counts[status] = count
    return result


@router.get("/overview")
def admin_overview(request: Request) -> dict[str, object]:
    now = _now()
    try:
        task_counts_sql = (
            "SELECT w.run_id, w.task, w.status, COUNT(*) AS total FROM refresh_work_items w "
            "JOIN (SELECT run_id FROM refresh_runs ORDER BY created_at DESC, run_id DESC LIMIT 1) latest ON latest.run_id = w.run_id "
            "GROUP BY w.run_id, w.task, w.status"
        )
        latest, successful, active, provider_captures, latest_item_rows = _read_batch([
            ("SELECT run_id, cycle_id, state, force, manifest_version, created_at, updated_at, finished_at FROM refresh_runs ORDER BY created_at DESC, run_id DESC LIMIT 1", ()),
            ("SELECT run_id, cycle_id, state, force, manifest_version, created_at, updated_at, finished_at FROM refresh_runs WHERE state = 'succeeded' ORDER BY created_at DESC, run_id DESC LIMIT 1", ()),
            ("SELECT COUNT(*) AS total FROM refresh_runs WHERE state = 'running'", ()),
            (_PROVIDER_CAPTURE_SQL, ()),
            (task_counts_sql, ()),
        ])
        latest_counts = _task_counts_from_rows(latest_item_rows)
    except (sqlite3.Error, psycopg.Error, OSError):
        raise HTTPException(503, detail={"error": "admin_data_unavailable"}) from None
    latest_run = _run_summary(latest[0], latest_counts.get(str(latest[0]["run_id"]), {})) if latest else None
    successful_run = _run_summary(successful[0]) if successful else None
    return {
        "generated_at": now.isoformat().replace("+00:00", "Z"),
        "as_of": now.isoformat().replace("+00:00", "Z"),
        "scope": "persisted_refresh_runs_and_receipts",
        "latest_attempt": latest_run,
        "latest_fully_successful": successful_run,
        "active_runs": {"count": _int(active[0].get("total")) if active else 0, "scope": "persisted_state_running"},
        "latest_valid_captures": {str(row["provider"]): _timestamp(row.get("captured_at")) for row in provider_captures if isinstance(row.get("provider"), str) and row["provider"] in _PROVIDERS},
        "state": "recorded" if latest_run else "no_backend_run_record",
        "provider_capture_scope": "See /api/admin/providers; only validated persisted receipts count as captures.",
    }


@router.get("/runs", response_model=RunPage)
def admin_runs(
    request: Request,
    limit: int = Query(DEFAULT_LIMIT, ge=1, le=MAX_LIMIT),
    since: str | None = Query(None, max_length=64),
    until: str | None = Query(None, max_length=64),
    state: RunState | None = None,
    cursor: str | None = Query(None, max_length=MAX_CURSOR),
) -> RunPage:
    generated = _now()
    period_filters: dict[str, object] = {"limit": limit, "state": state.value if state else None}
    after = _cursor_decode(cursor, period_filters) if cursor else None
    if after:
        cursor_since = _parse_bound(str(after["since"]), "cursor")
        requested_upper = _parse_bound(str(after["until"]), "cursor")
        if since is not None and _parse_bound(since, "since") != cursor_since:
            raise HTTPException(422, detail={"error": "cursor_filter_mismatch"})
        if until is not None and _parse_bound(until, "until") != requested_upper:
            raise HTTPException(422, detail={"error": "cursor_filter_mismatch"})
        lower = cursor_since
    else:
        requested_upper = _parse_bound(until, "until") if until is not None else generated
        upper_bound = min(requested_upper, generated)
        lower = _parse_bound(since, "since") if since is not None else upper_bound - timedelta(days=DEFAULT_DAYS)
    if lower >= requested_upper:
        raise HTTPException(422, detail={"error": "invalid_period"})
    anchor = _parse_bound(str(after["anchor"]), "cursor") if after else generated
    if after and anchor > generated:
        raise HTTPException(422, detail={"error": "invalid_cursor"})
    upper = min(requested_upper, anchor)
    if lower >= upper:
        raise HTTPException(422, detail={"error": "invalid_period"})
    expected: dict[str, object] = {**period_filters, "since": lower.isoformat(), "until": requested_upper.isoformat()}
    expected["anchor"] = anchor.isoformat()
    order_expr = _db_time_expr("created_at")
    upper_sql = "created_at::timestamptz < ?::timestamptz" if store.uses_postgres() else "julianday(created_at) < julianday(?)"
    lower_sql = "created_at::timestamptz >= ?::timestamptz" if store.uses_postgres() else "julianday(created_at) >= julianday(?)"
    params: list[object] = [lower.isoformat(), upper.isoformat()]
    clauses = [f"{lower_sql}", f"{upper_sql}"]
    clauses.append("created_at::timestamptz <= ?::timestamptz" if store.uses_postgres() else "julianday(created_at) <= julianday(?)")
    params.append(anchor.isoformat())
    if state is not None:
        clauses.append("state = ?")
        params.append(state.value)
    if after:
        cursor_time = str(after["created_at"])
        if store.uses_postgres():
            clauses.append(f"({order_expr} < ?::timestamptz OR ({order_expr} = ?::timestamptz AND run_id < ?))")
        else:
            clauses.append(f"({order_expr} < julianday(?) OR ({order_expr} = julianday(?) AND run_id < ?))")
        params.extend((cursor_time, cursor_time, str(after["run_id"])) )
    params.append(limit + 1)
    try:
        rows = _read(
            "SELECT run_id, cycle_id, state, force, manifest_version, created_at, updated_at, finished_at "
            "FROM refresh_runs WHERE " + " AND ".join(clauses) +
            f" ORDER BY {order_expr} DESC, run_id DESC LIMIT ?", tuple(params),
        )
        has_more = len(rows) > limit
        page_rows = rows[:limit]
        ids = [str(row["run_id"]) for row in page_rows]
        counts = _task_counts_from_rows(_read(
            f"SELECT run_id, task, status, COUNT(*) AS total FROM refresh_work_items WHERE run_id IN ({','.join('?' for _ in ids)}) GROUP BY run_id, task, status",
            tuple(ids),
        )) if ids else {}
    except (sqlite3.Error, psycopg.Error, OSError):
        raise HTTPException(503, detail={"error": "admin_data_unavailable"}) from None
    items = [_run_summary(row, counts.get(str(row["run_id"]), {})) for row in page_rows]
    next_cursor = None
    if has_more and page_rows:
        last = page_rows[-1]
        created_at = _timestamp(last.get("created_at"))
        if created_at is None:
            raise HTTPException(503, detail={"error": "admin_data_unavailable"})
        next_cursor = _cursor_encode({**expected, "created_at": created_at, "run_id": str(last["run_id"])})
    return RunPage(generated_at=generated.isoformat().replace("+00:00", "Z"), as_of=upper.isoformat().replace("+00:00", "Z"), scope="refresh_runs_created_in_half_open_period", items=items, limit=limit, has_more=has_more, next_cursor=next_cursor)


def _parse_bound(value: str, field: str) -> datetime:
    parsed = _timestamp(value)
    if parsed is None:
        raise HTTPException(422, detail={"error": f"invalid_{field}"})
    return datetime.fromisoformat(parsed.replace("Z", "+00:00"))


def _safe_final_summary(value: object) -> dict[str, object]:
    if not isinstance(value, str) or len(value.encode("utf-8", errors="ignore")) > MAX_FINAL_JSON_BYTES:
        return {"status": "not_recorded", "task_count": None}
    try:
        decoded_raw: object = json.loads(value)
    except (ValueError, RecursionError):
        return {"status": "metadata_unknown", "task_count": None}
    if not isinstance(decoded_raw, dict):
        return {"status": "metadata_unknown", "task_count": None}
    decoded = cast(dict[str, object], decoded_raw)
    status = decoded.get("status")
    if status not in {"ok", "partial", "error"}:
        return {"status": "metadata_unknown", "task_count": None}
    tasks_raw = decoded.get("tasks")
    tasks = cast(list[object], tasks_raw) if isinstance(tasks_raw, list) else None
    if not isinstance(tasks, list) or len(tasks) > 1000:
        return {"status": "metadata_unknown", "task_count": None}
    return {"status": status, "task_count": len(tasks)}


def _task_summary(row: dict[str, object]) -> dict[str, object]:
    task = row.get("task")
    item_key = row.get("item_key")
    status = _safe_state(row.get("status"), WorkState)
    category = row.get("last_error_category")
    return {
        "item_key": _safe_identifier(item_key, 128) or "metadata_unknown",
        "task": _safe_identifier(task, 128) or "metadata_unknown",
        "task_order": _int(row.get("task_order")),
        "status": status,
        "attempt_count": _int(row.get("attempt")),
        "claimed_at": _timestamp(row.get("claimed_at")),
        "created_at": _timestamp(row.get("created_at")),
        "finished_at": _timestamp(row.get("finished_at")),
        "duration_seconds": _duration(_timestamp(row.get("claimed_at")) or _timestamp(row.get("created_at")), _timestamp(row.get("finished_at"))),
        "series_updated": _int(row.get("series_updated")),
        "record_count": _int(row.get("record_count")),
        "next_attempt_at": _timestamp(row.get("next_attempt_at")),
        "deferral_reason": row.get("deferral_reason") if row.get("deferral_reason") in {"publisher_cooldown", "source_dependency_deferred"} else ("metadata_unknown" if row.get("deferral_reason") else None),
        "last_error_category": category if isinstance(category, str) and category in _CATEGORIES else ("metadata_unknown" if category else None),
        "error_code": _safe_error_code(row.get("last_error_code") or row.get("error_code")),
    }


def _safe_error_code(value: object) -> str | None:
    return value if isinstance(value, str) and value in _SAFE_ERROR_CODES else ("metadata_unknown" if value is not None else None)


@router.get("/runs/{run_id}")
def admin_run_detail(
    request: Request,
    run_id: str,
    task_limit: int = Query(DEFAULT_LIMIT, ge=1, le=MAX_LIMIT),
    task_cursor: int | None = Query(None, ge=0),
) -> dict[str, object]:
    if not _RUN_ID.fullmatch(run_id):
        raise HTTPException(422, detail={"error": "invalid_run_id"})
    generated = _now()
    try:
        with read_transaction() as connection:
            if store.uses_postgres():
                execute(connection, "SET TRANSACTION READ ONLY")
            run_cursor = execute(connection, "SELECT run_id, cycle_id, state, force, manifest_version, derived_cutoff, macro_cutoff, created_at, updated_at, finished_at, final_result FROM refresh_runs WHERE run_id = ?", (run_id,))
            record = run_cursor.fetchone()
            if record is None:
                raise HTTPException(404, detail={"error": "admin_run_not_found"})
            offset_key = task_cursor if task_cursor is not None else -1
            task_cursor_result = execute(connection,
                "SELECT item_key, task, task_order, status, attempt, claimed_at, created_at, finished_at, series_updated, record_count, next_attempt_at, deferral_reason, last_error_category, last_error_code, error_code "
                "FROM refresh_work_items WHERE run_id = ? AND task_order > ? ORDER BY task_order LIMIT ?",
                (run_id, offset_key, task_limit + 1))
            task_rows = [_row_dict(row) for row in task_cursor_result.fetchall()]
            count_cursor = execute(connection, "SELECT status, COUNT(*) AS total FROM refresh_work_items WHERE run_id = ? GROUP BY status", (run_id,))
            task_count_rows = [_row_dict(task_row) for task_row in count_cursor.fetchall()]
            plan_cursor = execute(connection, "SELECT plan_version, as_of, plan_sha256 FROM sgs_refresh_plans WHERE run_id = ?", (run_id,))
            plan_row = plan_cursor.fetchone()
    except HTTPException:
        raise
    except (sqlite3.Error, psycopg.Error, OSError):
        raise HTTPException(503, detail={"error": "admin_data_unavailable"}) from None
    row = _row_dict(record)
    has_more = len(task_rows) > task_limit
    visible_tasks = task_rows[:task_limit]
    next_task_cursor = visible_tasks[-1].get("task_order") if has_more and visible_tasks else None
    try:
        diagnostics = store.acquisition_diagnostics_for_refresh_run(run_id)
        safe_diagnostics = sanitize_public_summary(summarize_refresh_run_diagnostics(run_id, diagnostics), run_id)
    except (sqlite3.Error, psycopg.Error, OSError, ValueError):
        from acquisition_diagnostics import unavailable_summary
        safe_diagnostics = unavailable_summary(run_id, "not_recorded")
    task_counts: dict[str, int] = {}
    for task_count in task_count_rows:
        status_value = _safe_state(task_count.get("status"), WorkState)
        count_value = _int(task_count.get("total"))
        if count_value is not None:
            task_counts[status_value] = count_value
    total = sum(task_counts.values())
    plan_metadata = _row_dict(plan_row) if plan_row is not None else {}
    raw_plan_version = plan_metadata.get("plan_version")
    plan_version = _safe_identifier(raw_plan_version, 80) or ("metadata_unknown" if plan_row is not None else "not_recorded")
    plan_as_of = _timestamp(plan_metadata.get("as_of"))
    plan_hash = plan_metadata.get("plan_sha256")
    if not isinstance(plan_hash, str) or re.fullmatch(r"[a-f0-9]{64}", plan_hash) is None:
        plan_hash = "metadata_unknown" if plan_row is not None else None
    return {
        "generated_at": generated.isoformat().replace("+00:00", "Z"),
        "as_of": generated.isoformat().replace("+00:00", "Z"),
        "scope": "selected_persisted_run_snapshot; diagnostics_exact_run_only",
        "run": {**_run_summary(row), "derived_cutoff": _timestamp(row.get("derived_cutoff")), "macro_cutoff": _timestamp(row.get("macro_cutoff")), "recorded_final_summary": _safe_final_summary(row.get("final_result")), "sgs_plan": {"version": plan_version, "as_of": plan_as_of, "sha256": plan_hash}},
        "task_page": {"items": [_task_summary(item) for item in visible_tasks], "total": total, "counts_by_status": task_counts, "limit": task_limit, "has_more": has_more, "next_cursor": next_task_cursor},
        "diagnostics": safe_diagnostics,
        "current_freshness": {"status": "not_included", "reason": "historical_run_snapshot_is_frozen"},
    }


def _source_scope(provider: str) -> str:
    return f"{provider}:primary:primary"


@router.get("/providers")
def admin_providers(request: Request) -> dict[str, object]:
    now = _now()
    as_of = now.isoformat().replace("+00:00", "Z")
    placeholders = ",".join("?" for _ in _PROVIDERS)
    try:
        states, captures, failures = _read_batch([
            (f"SELECT scope_id, state, opened_until, retry_not_before, probe_lease_until, updated_at FROM source_access_state WHERE scope_id IN ({placeholders})", tuple(_source_scope(p) for p in _PROVIDERS)),
            (_PROVIDER_CAPTURE_SQL, ()),
            (f"SELECT source, error_category FROM (SELECT source, error_category, ROW_NUMBER() OVER (PARTITION BY source ORDER BY started_at DESC, extraction_id DESC) AS position FROM source_extractions WHERE source IN ({placeholders}) AND status IN ('network_error','validation_error','persistence_error','freshness_error','logging_error')) latest_failures WHERE position = 1", _PROVIDERS),
        ])
    except (sqlite3.Error, psycopg.Error, OSError):
        raise HTTPException(503, detail={"error": "admin_data_unavailable"}) from None
    state_by_scope = {str(row.get("scope_id")): row for row in states}
    capture_by_source = {str(row.get("provider")): _timestamp(row.get("captured_at")) for row in captures}
    failure_by_source: dict[str, str] = {}
    for row in failures:
        source = row.get("source")
        category = row.get("error_category")
        if isinstance(source, str) and source not in failure_by_source:
            failure_by_source[source] = category if isinstance(category, str) and category in _CATEGORIES else "metadata_unknown"
    items: list[dict[str, object]] = []
    for provider in _PROVIDERS:
        row = state_by_scope.get(_source_scope(provider))
        raw_state = row.get("state") if row else None
        state = raw_state if raw_state in {"closed", "open", "half_open"} else ("not_recorded" if row is None else "metadata_unknown")
        due = max((parsed for parsed in (_timestamp(row.get("opened_until")) if row else None, _timestamp(row.get("retry_not_before")) if row else None) if parsed), default=None)
        if state in {"open", "half_open"} and due and datetime.fromisoformat(due.replace("Z", "+00:00")) <= now:
            admission = "next_admission_reassessment_due"
        elif state == "open":
            admission = "temporarily_deferred"
        elif state == "half_open":
            admission = "probe_lease_active" if row is not None and _timestamp(row.get("probe_lease_until")) else "metadata_unknown"
        else:
            admission = "not_deferred" if state == "closed" else "metadata_unknown"
        capture = capture_by_source.get(provider)
        items.append({"provider": provider, "scope_id": _source_scope(provider), "circuit": {"recorded_state": state, "opened_until": _timestamp(row.get("opened_until")) if row is not None else None, "retry_not_before": _timestamp(row.get("retry_not_before")) if row is not None else None, "probe_lease_until": _timestamp(row.get("probe_lease_until")) if row is not None else None, "admission_view": admission, "as_of": as_of}, "last_valid_capture_at": capture, "capture_status": "recorded" if capture else "not_recorded", "latest_acquisition_failure_category": failure_by_source.get(provider), "diagnostics_status": "not_included"})
    return {"generated_at": as_of, "as_of": as_of, "scope": "curated_provider_registry_and_persisted_receipts", "items": items}


def _coverage_catalog() -> list[IndicatorSpec]:
    specs: dict[int, IndicatorSpec] = {}
    for spec in INDICATOR_CATALOG.values():
        if spec.provider.value != "bcb_sgs":
            continue
        code = int(spec.provider_code)
        if code in specs and getattr(specs[code], "key", None) != spec.key:
            continue
        specs[code] = spec
    return [specs[key] for key in sorted(specs)]


@router.get("/coverage")
def admin_coverage(
    request: Request,
    provider: Annotated[ProviderCode, Query()],
    limit: int = Query(DEFAULT_LIMIT, ge=1, le=MAX_LIMIT),
    cursor: int | None = Query(None, ge=0),
) -> dict[str, object]:
    now = _now()
    generated = now.isoformat().replace("+00:00", "Z")
    if provider is not ProviderCode.BCB_SGS:
        if cursor not in (None, 0):
            raise HTTPException(422, detail={"error": "invalid_cursor"})
        return _non_sgs_coverage(provider, limit, generated)
    specs = _coverage_catalog()
    start_index = cursor or 0
    if start_index > len(specs):
        raise HTTPException(422, detail={"error": "invalid_cursor"})
    page: list[IndicatorSpec] = specs[start_index:start_index + limit]
    page_codes = [int(spec.provider_code) for spec in page]
    if not page_codes:
        return {"generated_at": generated, "as_of": generated, "scope": "current_bcb_sgs_catalog_and_exact_receipts", "provider": provider.value, "coverage_status": "catalog_and_receipt_evidence", "item_count": 0, "items": [], "limit": limit, "has_more": False, "next_cursor": None}
    try:
        captures, proofs = _coverage_reads(page_codes)
    except (sqlite3.Error, psycopg.Error, OSError):
        raise HTTPException(503, detail={"error": "admin_data_unavailable"}) from None
    items = _coverage_items(page, captures, proofs, now, generated)
    has_more = start_index + len(page) < len(specs)
    next_cursor = start_index + len(page) if has_more else None
    return {"generated_at": generated, "as_of": generated, "scope": "current_bcb_sgs_catalog_and_exact_receipts; no_calendar_gap_inference", "provider": provider.value, "coverage_status": "catalog_and_receipt_evidence", "item_count": len(specs), "items": items, "limit": limit, "has_more": has_more, "next_cursor": next_cursor, "cursor_order": "catalog provider_code ascending"}


def _non_sgs_coverage(provider: ProviderCode, limit: int, generated: str) -> dict[str, object]:
    try:
        capture_rows = _read(_NON_SGS_CAPTURE_SQL, (provider.value, provider.value))
        reference_rows = _read(
            "SELECT t.reference_max, t.captured_at FROM treasury_cohort_commits t "
            "JOIN source_extractions e ON e.extraction_id = t.extraction_id "
            "WHERE e.source = ? ORDER BY t.captured_at DESC, t.extraction_id DESC LIMIT 1",
            (provider.value,),
        ) if provider is ProviderCode.TESOURO else []
    except (sqlite3.Error, psycopg.Error, OSError):
        raise HTTPException(503, detail={"error": "admin_data_unavailable"}) from None
    latest_capture = _timestamp(capture_rows[0].get("captured_at")) if capture_rows else None
    reference = _safe_date(reference_rows[0].get("reference_max")) if reference_rows else None
    reference_capture = _timestamp(reference_rows[0].get("captured_at")) if reference_rows else None
    return {
        "generated_at": generated,
        "as_of": generated,
        "scope": "recognized_provider_persisted_capture_facts; no_provider_series_catalog_or_completeness_contract",
        "provider": provider.value,
        "coverage_status": "metadata_unknown",
        "reason": "provider_specific_series_coverage_contract_not_available",
        "latest_valid_capture_at": latest_capture,
        "capture_status": "recorded" if latest_capture else "not_recorded",
        "capture_basis": "existing_curated_provider_receipt_projection",
        "latest_reference": reference,
        "latest_reference_captured_at": reference_capture,
        "latest_reference_basis": "treasury_cohort_commit.reference_max" if reference is not None else "not_recorded",
        "item_count": None,
        "item_count_status": "metadata_unknown",
        "items": [],
        "items_status": "not_recorded",
        "limit": limit,
        "has_more": False,
        "next_cursor": None,
    }


def _candidate_union(
    table: str,
    columns: str,
    filter_sql: str,
    order_sql: str,
    page_codes: list[int],
    filter_params: tuple[object, ...] = (),
) -> tuple[str, tuple[object, ...]]:
    """Build fixed-dialect-safe, indexed per-series top-N seeks in one statement."""
    branches: list[str] = []
    params: list[object] = []
    for code in page_codes:
        branches.append(
            f"SELECT {columns} FROM (SELECT {columns} FROM {table} "
            f"WHERE series_code = ? {filter_sql} ORDER BY {order_sql} "
            "LIMIT ?) bounded_series_candidates"
        )
        params.extend((code, *filter_params, _COVERAGE_CANDIDATE_LIMIT))
    return " UNION ALL ".join(branches), tuple(params)


def _coverage_capture_query(page_codes: list[int]) -> tuple[str, tuple[object, ...]]:
    columns = (
        "series_code, operation_id, extraction_id, run_id, unit_id, plan_sha256, contract_id, "
        "contract_version, identity_kind, scope_role, policy_version, requested_start, requested_end, "
        "observed_count, reference_start, reference_end, captured_at"
    )
    candidates, params = _candidate_union(
        "sgs_unit_commits", columns, "", "captured_at DESC, operation_id DESC", page_codes,
    )
    selected = ",".join("(?)" for _ in page_codes)
    sql = f"""WITH selected_series(series_code) AS (VALUES {selected}),
        candidates AS MATERIALIZED ({candidates}),
        candidate_counts AS (SELECT series_code, COUNT(*) AS candidate_count FROM candidates GROUP BY series_code),
        valid_captures AS (
            SELECT c.series_code, c.captured_at, c.reference_start, c.reference_end,
                   ROW_NUMBER() OVER (PARTITION BY c.series_code ORDER BY c.captured_at DESC, c.operation_id DESC) AS position
            FROM candidates c
            JOIN sgs_refresh_plans p ON p.run_id = c.run_id AND p.plan_sha256 = c.plan_sha256
            JOIN sgs_refresh_plan_units u ON u.run_id = c.run_id AND u.unit_id = c.unit_id
                AND u.series_code = c.series_code AND u.contract_version = c.contract_version
                AND u.identity_kind = c.identity_kind AND u.role = c.scope_role
                AND u.policy_version = c.policy_version AND u.start_date = c.requested_start
                AND u.end_date = c.requested_end
            JOIN source_extractions e ON e.extraction_id = c.extraction_id AND e.run_id = c.run_id
                AND e.task_key = ('sgs_plan_' || replace(u.unit_id, ':', '_'))
            WHERE c.contract_id = ('sgs:' || CAST(c.series_code AS TEXT)) AND c.observed_count > 0
        )
        SELECT s.series_code, COALESCE(n.candidate_count, 0) AS candidate_count,
               v.captured_at, v.reference_start, v.reference_end
        FROM selected_series s LEFT JOIN candidate_counts n ON n.series_code = s.series_code
        LEFT JOIN valid_captures v ON v.series_code = s.series_code AND v.position = 1
        ORDER BY s.series_code"""
    return sql, (*page_codes, *params)


def _coverage_proof_query(page_codes: list[int]) -> tuple[str, tuple[object, ...]]:
    columns = (
        "operation_id, run_id, unit_id, source, series_code, contract_id, contract_version, "
        "identity_kind, scope_role, policy_version, requested_start, requested_end, payload_sha256, "
        "observed_count, captured_at, reference_start, reference_end, business_receipt_id"
    )
    role_filter = "AND source = 'bcb_sgs' AND scope_role IN ('archive_backfill','archive_reconcile') AND policy_version IN (" + ",".join("?" for _ in SUPPORTED_POLICY_VERSIONS) + ")"
    candidates, params = _candidate_union(
        "sgs_coverage_receipts", columns, role_filter,
        "requested_start, requested_end, captured_at, operation_id", page_codes,
        tuple(SUPPORTED_POLICY_VERSIONS),
    )
    selected = ",".join("(?)" for _ in page_codes)
    sql = f"""WITH selected_series(series_code) AS (VALUES {selected}),
        candidates AS MATERIALIZED ({candidates}),
        candidate_counts AS (SELECT series_code, COUNT(*) AS candidate_count FROM candidates GROUP BY series_code),
        validated AS (
            SELECT r.series_code, r.requested_start, r.requested_end, r.captured_at,
                   r.reference_start, r.reference_end,
                   ROW_NUMBER() OVER (PARTITION BY r.series_code ORDER BY r.requested_start,
                       r.requested_end, r.captured_at, r.operation_id) AS proof_position,
                   COUNT(*) OVER (PARTITION BY r.series_code) AS valid_count
            FROM candidates r
            JOIN sgs_unit_commits c ON c.operation_id = r.operation_id
                AND c.extraction_id = r.business_receipt_id AND c.run_id = r.run_id
                AND c.unit_id = r.unit_id AND c.series_code = r.series_code
                AND c.contract_id = r.contract_id AND c.contract_version = r.contract_version
                AND c.identity_kind = r.identity_kind AND c.scope_role = r.scope_role
                AND c.policy_version = r.policy_version AND c.requested_start = r.requested_start
                AND c.requested_end = r.requested_end AND c.payload_sha256 = r.payload_sha256
                AND c.observed_count = r.observed_count AND c.reference_start = r.reference_start
                AND c.reference_end = r.reference_end AND c.captured_at = r.captured_at
            JOIN sgs_refresh_plans p ON p.run_id = c.run_id AND p.plan_sha256 = c.plan_sha256
            JOIN sgs_refresh_plan_units u ON u.run_id = c.run_id AND u.unit_id = c.unit_id
                AND u.series_code = c.series_code AND u.contract_version = c.contract_version
                AND u.identity_kind = c.identity_kind AND u.role = c.scope_role
                AND u.policy_version = c.policy_version AND u.start_date = c.requested_start
                AND u.end_date = c.requested_end
        ),
        bounded_valid AS (SELECT * FROM validated WHERE proof_position <= 11)
        SELECT s.series_code, COALESCE(n.candidate_count, 0) AS candidate_count,
               COALESCE(v.valid_count, 0) AS valid_count, v.proof_position,
               v.requested_start, v.requested_end, v.captured_at,
               v.reference_start, v.reference_end
        FROM selected_series s LEFT JOIN candidate_counts n ON n.series_code = s.series_code
        LEFT JOIN bounded_valid v ON v.series_code = s.series_code
        ORDER BY s.series_code, v.proof_position"""
    return sql, (*page_codes, *params)


def _coverage_reads(page_codes: list[int]) -> tuple[list[dict[str, object]], list[dict[str, object]]]:
    capture_query = _coverage_capture_query(page_codes)
    proof_query = _coverage_proof_query(page_codes)
    captures, proofs = _read_batch([capture_query, proof_query])
    return captures, proofs


def _coverage_items(
    page: list[IndicatorSpec],
    captures: list[dict[str, object]],
    proofs: list[dict[str, object]],
    now: datetime,
    generated: str,
) -> list[dict[str, object]]:
    latest: dict[int, dict[str, object]] = {}
    capture_search_incomplete: set[int] = set()
    capture_candidate_counts: dict[int, int] = {}
    for row in captures:
        code = row.get("series_code")
        if type(code) is not int:
            continue
        candidate_count = _int(row.get("candidate_count")) or 0
        capture_candidate_counts[code] = candidate_count
        if row.get("captured_at") is not None:
            latest[code] = row
        elif candidate_count >= _COVERAGE_CANDIDATE_LIMIT:
            capture_search_incomplete.add(code)
    intervals: dict[int, list[dict[str, object]]] = {}
    truncated_codes: set[int] = set()
    proof_search_incomplete: set[int] = set()
    proof_counts: dict[int, int] = {}
    proof_candidate_counts: dict[int, int] = {}
    for row in proofs:
        code = row.get("series_code")
        if type(code) is not int:
            continue
        candidate_count = _int(row.get("candidate_count")) or 0
        proof_candidate_counts[code] = candidate_count
        valid_count = _int(row.get("valid_count")) or 0
        proof_counts[code] = valid_count
        if candidate_count >= _COVERAGE_CANDIDATE_LIMIT:
            proof_search_incomplete.add(code)
        if row.get("requested_start") is None:
            continue
        values = intervals.setdefault(code, [])
        if len(values) < 10:
            values.append({"requested_start": _safe_date(row.get("requested_start")), "requested_end": _safe_date(row.get("requested_end")), "reference_start": _safe_date(row.get("reference_start")), "reference_end": _safe_date(row.get("reference_end")), "captured_at": _timestamp(row.get("captured_at")), "basis": "exact_validated_sgs_archive_receipt"})
        if valid_count > 10:
            truncated_codes.add(code)
    items: list[dict[str, object]] = []
    for spec in page:
        code = int(spec.provider_code)
        capture = latest.get(code)
        reference = _safe_date(capture.get("reference_end")) if capture else None
        captured_at = _timestamp(capture.get("captured_at")) if capture else None
        availability = _availability_metadata(code, spec.frequency.value)
        statuses: list[str] = []
        if reference is None:
            if code not in capture_search_incomplete:
                statuses.append("acquisition_unverified")
            else:
                statuses.append("metadata_unknown")
        elif availability is not None:
            documented = availability["documented_start"]
            if date.fromisoformat(reference) < date.fromisoformat(documented):
                statuses.append("before_documented_start")
            elif (now.date() - date.fromisoformat(reference)).days > spec.freshness_days:
                statuses.append("stale_reference")
        elif (now.date() - date.fromisoformat(reference)).days > spec.freshness_days:
            statuses.append("stale_reference")
        if code in proof_search_incomplete:
            statuses.append("metadata_unknown")
        elif proof_counts.get(code, 0) == 0:
            statuses.append("history_pending")
        documented_start = None
        if availability is None:
            statuses.append("metadata_unknown")
        else:
            documented_start = availability.get("documented_start")
        proof_candidates = proof_candidate_counts.get(code, 0)
        capture_candidates = capture_candidate_counts.get(code, 0)
        incomplete = code in proof_search_incomplete or code in capture_search_incomplete
        items.append({"series_code": code, "series_key": str(spec.key), "name": spec.label, "frequency": spec.frequency.value, "unit": spec.unit.value, "latest_reference": reference, "last_valid_capture_at": captured_at, "capture_search": {"status": "metadata_unknown" if code in capture_search_incomplete else ("recorded" if capture else "not_recorded"), "candidate_budget": COVERAGE_CANDIDATE_BUDGET, "candidates_examined": capture_candidates, "budget_sentinel_present": capture_candidates > COVERAGE_CANDIDATE_BUDGET}, "freshness_days": spec.freshness_days, "configured_start": _safe_date(spec.start_date), "documented_start": documented_start, "first_reference_period": availability.get("first_reference_period") if availability else None, "coverage_status": "metadata_unknown" if incomplete or availability is None else "evidence_available", "coverage_reason": "candidate_search_budget_exhausted" if incomplete else ("availability_metadata_not_available" if availability is None else None), "status_flags": sorted(set(statuses)) or ["recorded"], "evidence_basis": "exact_plan_bound_sgs_unit_commit_and_archive_receipts", "acquired_proof_intervals": intervals.get(code, []), "proof_intervals_truncated": code in truncated_codes, "proof_search": {"status": "metadata_unknown" if code in proof_search_incomplete else "complete", "candidate_budget": COVERAGE_CANDIDATE_BUDGET, "candidates_examined": proof_candidates, "budget_sentinel_present": proof_candidates > COVERAGE_CANDIDATE_BUDGET}, "history_status": "metadata_unknown" if code in proof_search_incomplete or availability is None else ("history_pending" if proof_counts.get(code, 0) == 0 else "receipt_intervals_only_no_gap_calendar_inference"), "as_of": generated})
    return items


def _safe_date(value: object) -> str | None:
    if isinstance(value, datetime):
        return value.date().isoformat()
    if not isinstance(value, (str, date)):
        return None
    try:
        parsed = value if isinstance(value, date) else date.fromisoformat(value[:10])
    except ValueError:
        return None
    return parsed.isoformat()


def _availability_metadata(code: int, frequency: str) -> dict[str, str] | None:
    from db.sgs_availability import availability_for
    metadata = availability_for(code, frequency)
    if metadata is None:
        return None
    return {"first_reference_period": metadata.first_reference_period.isoformat(), "documented_start": metadata.documented_start.isoformat(), "registry_version": metadata.registry_version}
