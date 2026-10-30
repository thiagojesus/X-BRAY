"""Safe public projection of persisted source-request diagnostics."""

from __future__ import annotations

import json
import re
from collections.abc import Mapping
from datetime import date, datetime, timezone
from typing import Final, cast

from source_diagnostics import DIAGNOSTICS_VERSION, bounded_diagnostic

SCHEMA_VERSION: Final = "acquisition-diagnostics-v1"
MAX_REPORT_ITEMS: Final = 10
MAX_REQUESTS_PER_ITEM: Final = 3
MAX_TRACE_JSON_BYTES: Final = 512 * 1024
_SAFE_ID = re.compile(r"[A-Za-z0-9_.:-]{1,128}\Z")
_LEGACY_ERRORS: Final = {
    "InvalidSGSPayloadError": "invalid_sgs_payload",
    "ProviderInvalidJSONResponseError": "invalid_json",
    "ProviderResponseDecodingError": "content_decoding_error",
    "ReadTimeout": "read_timeout",
    "ConnectTimeout": "connect_timeout",
    "WriteTimeout": "write_timeout",
    "PoolTimeout": "pool_timeout",
    "HTTPStatusError": "http_status_error",
    "RuntimeError": "unknown",
    "TimeoutError": "timeout",
}
_LEGACY_ERROR_CODES: Final = frozenset(_LEGACY_ERRORS.values()) | {"unknown"}
_VALIDATION_REASONS: Final = {
    "expected-list": "expected_list",
    "expected-record": "expected_record",
    "invalid-value": "invalid_value",
    "invalid-date": "invalid_date",
    "outside-requested-window": "outside_requested_window",
    "invalid-period-end": "invalid_period_end",
    "invalid-period": "invalid_period",
    "conflicting-duplicate-period": "conflicting_duplicate_period",
    "conflicting-duplicate-date": "conflicting_duplicate_date",
    "empty-cohort": "empty_cohort",
}
_SAFE_VALIDATION_REASONS: Final = frozenset(_VALIDATION_REASONS.values()) | {"unknown"}
_CATEGORIES: Final = frozenset({
    "network_timeout", "network_reset", "temporary_server_error", "rate_limited",
    "envelope_error", "invalid_schema", "semantic_conflict", "incomplete_coverage",
    "persistence_failure", "freshness_failure", "logging_failure", "configuration_error",
    "authentication_error", "forbidden_host", "internal_error", "deferred", "circuit_open",
})
_EXTRACTION_STATUSES: Final = frozenset({
    "running", "success", "partial", "cache_hit", "empty", "network_error",
    "validation_error", "persistence_error", "freshness_error", "logging_error",
})
_DIAG_CODES: Final = frozenset({
    "connect_timeout", "read_timeout", "write_timeout", "pool_timeout", "timeout",
    "dns_resolution_error", "tls_error", "connect_error", "read_error", "write_error",
    "connection_reset", "invalid_json", "invalid_json_shape", "protocol_error",
    "content_decoding_error", "http_status_error", "unknown", "diagnostics_truncated",
    "diagnostics_unavailable",
})
_SUMMARY_STATUSES: Final = frozenset({"recorded", "not_recorded", "unsupported_version", "invalid", "truncated", "run_mismatch"})
_MIME_TYPES: Final = frozenset({
    "application/json", "application/problem+json", "application/octet-stream",
    "text/plain", "text/html", "application/xml", "text/xml",
})


def _mapping(value: object) -> Mapping[str, object] | None:
    return cast(Mapping[str, object], value) if isinstance(value, Mapping) else None


def _safe_id(value: object) -> str | None:
    return value if isinstance(value, str) and _SAFE_ID.fullmatch(value) else None


def _safe_time(value: object) -> str | None:
    if not isinstance(value, str) or len(value) > 64:
        return None
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        return None
    return parsed.astimezone(timezone.utc).isoformat()


def _json_traces(value: object) -> tuple[list[object], str | None]:
    if not isinstance(value, str):
        return [], "not_recorded"
    if len(value.encode("utf-8", errors="ignore")) > MAX_TRACE_JSON_BYTES:
        return [], "invalid"
    try:
        decoded: object = json.loads(value)
    except (ValueError, RecursionError):
        return [], "invalid"
    if not isinstance(decoded, list):
        return [], "invalid"
    return cast(list[object], decoded[:256]), None


def _context(value: object) -> dict[str, object] | None:
    mapping = _mapping(value)
    if mapping is None:
        return None
    provider = mapping.get("provider")
    attempt = mapping.get("local_attempt")
    if type(attempt) is not int or not 1 <= attempt <= 16:
        return None
    if provider == "bcb_sgs":
        code = mapping.get("series_code")
        start = _safe_time_date(mapping.get("requested_start"))
        end = _safe_time_date(mapping.get("requested_end"))
        if type(code) is int and code > 0 and start and end and start <= end and mapping.get("expected_kind") == "list":
            return {
                "provider": "bcb_sgs", "series_code": code,
                "requested_start": start, "requested_end": end,
                "expected_kind": "list", "local_attempt": attempt,
            }
    if provider == "b3":
        trade_date = _safe_time_date(mapping.get("trade_date"))
        filename = mapping.get("file_name")
        if trade_date and isinstance(filename, str):
            parsed = date.fromisoformat(trade_date)
            expected = f"SPRD{parsed:%y%m%d}.zip"
            if filename == expected:
                return {"provider": "b3", "trade_date": trade_date, "file_name": expected, "local_attempt": attempt}
    return None


def _safe_time_date(value: object) -> str | None:
    if not isinstance(value, str) or len(value) != 10:
        return None
    try:
        parsed = date.fromisoformat(value)
    except ValueError:
        return None
    return parsed.isoformat() if parsed.isoformat() == value else None


def _trace_projection(
    trace_value: object, validation_reason: str | None = None
) -> tuple[dict[str, object] | None, str | None, bool]:
    trace = _mapping(trace_value)
    if trace is None:
        return None, "invalid", False
    raw_diag = _mapping(trace.get("diagnostics"))
    if raw_diag is None:
        return None, "not_recorded", False
    version = raw_diag.get("version")
    if version != DIAGNOSTICS_VERSION:
        return None, "unsupported_version" if isinstance(version, str) else "not_recorded", False
    safe = bounded_diagnostic(raw_diag)
    diagnostic = _mapping(safe)
    if diagnostic is None:
        return None, "invalid", False
    response = _mapping(diagnostic.get("response")) or {}
    status = response.get("status_code")
    failure = diagnostic.get("failure_code")
    response_validation_failure = validation_reason is not None and status == 200
    if not (
        (isinstance(failure, str) and failure in _DIAG_CODES)
        or (type(status) is int and status >= 400)
        or response_validation_failure
    ):
        return None, None, True
    projected: dict[str, object] = {
        "dispatch_sequence": trace.get("dispatch_sequence")
        if type(trace.get("dispatch_sequence")) is int and 1 <= cast(int, trace.get("dispatch_sequence")) <= 256 else None,
        "started_at": _safe_time(trace.get("dispatch_started_at")),
        "finished_at": _safe_time(trace.get("dispatch_finished_at")),
        "duration_ms": diagnostic.get("duration_ms"),
        "response": {
            "status_code": status,
            "content_type": response.get("content_type") if isinstance(response.get("content_type"), str) else "unknown",
            "body_kind": response.get("body_kind") if isinstance(response.get("body_kind"), str) else "unknown",
            "bytes_received": response.get("bytes_received") if type(response.get("bytes_received")) is int else None,
        },
    }
    if isinstance(failure, str) and failure in _DIAG_CODES:
        projected["failure_code"] = failure
    elif type(status) is int and status >= 400:
        projected["failure_code"] = "http_status_error"
    if response_validation_failure:
        projected["validation_reason"] = validation_reason
    context = _context(diagnostic.get("request_context"))
    if context is not None:
        projected["request_context"] = context
    return projected, None, False


def _row_projection(row_value: object) -> tuple[dict[str, object] | None, bool, str | None]:
    row = _mapping(row_value)
    if row is None:
        return None, False, "invalid"
    extraction_id = _safe_id(row.get("extraction_id"))
    task_key = _safe_id(row.get("task_key"))
    source_value = row.get("source")
    source = source_value if isinstance(source_value, str) and source_value in {"bcb_sgs", "b3"} else None
    if extraction_id is None or source is None:
        return None, False, "invalid"
    raw_traces, trace_problem = _json_traces(row.get("request_trace_json"))
    raw_code = row.get("error_code")
    raw_reason = row.get("error_detail")
    validation_reason = (
        _VALIDATION_REASONS.get(raw_reason, "unknown")
        if raw_code == "InvalidSGSPayloadError" and isinstance(raw_reason, str)
        else None
    )
    requests: list[dict[str, object]] = []
    saw_diag = False
    issues: list[str] = []
    for raw_trace in raw_traces:
        trace = _mapping(raw_trace)
        diagnostic = _mapping(trace.get("diagnostics")) if trace is not None else None
        if diagnostic is not None and diagnostic.get("version") == DIAGNOSTICS_VERSION:
            saw_diag = True
        request, issue, _recovered = _trace_projection(raw_trace, validation_reason)
        if issue:
            issues.append(issue)
        if request is not None:
            requests.append(request)
    status_value = row.get("status")
    status = status_value if isinstance(status_value, str) and status_value in _EXTRACTION_STATUSES else "unknown"
    legacy_code = _LEGACY_ERRORS.get(raw_code) if isinstance(raw_code, str) else None
    category_value = row.get("error_category")
    category = category_value if isinstance(category_value, str) and category_value in _CATEGORIES else None
    if not requests and status in {"success", "cache_hit", "empty"} and legacy_code is None:
        return None, saw_diag, trace_problem or (issues[0] if issues else None)
    item: dict[str, object] = {
        "source": source,
        "extraction_id": extraction_id,
        "task_key": task_key,
        "status": status,
        "started_at": _safe_time(row.get("started_at")),
        "finished_at": _safe_time(row.get("finished_at")),
        "legacy_error_code": legacy_code or ("unknown" if raw_code else None),
        "error_category": category,
        "diagnostics_status": "recorded" if saw_diag else (trace_problem or next(iter(issues), "not_recorded")),
        "requests": requests[:MAX_REQUESTS_PER_ITEM],
        "truncated_requests": max(0, len(requests) - MAX_REQUESTS_PER_ITEM),
    }
    attempt_relation = row.get("attempt_relation")
    if isinstance(attempt_relation, str) and attempt_relation in {"current", "previous", "unverified"}:
        item["attempt_relation"] = attempt_relation
    if status == "success" and requests:
        item["outcome"] = "recovered"
    for request in requests[:MAX_REQUESTS_PER_ITEM]:
        context = request.get("request_context")
        if context is not None:
            item["request_context"] = context
            break
    if validation_reason is not None:
        item["validation_reason"] = validation_reason
    return item, saw_diag, trace_problem or (issues[0] if issues else None)


def summarize_refresh_run_diagnostics(run_id: str, query: Mapping[str, object]) -> dict[str, object]:
    """Build a bounded per-run public summary without exposing stored raw fields."""
    safe_run_id = _safe_id(run_id)
    rows_value = query.get("rows")
    rows = cast(list[object], rows_value) if isinstance(rows_value, list) else []
    rows_truncated = query.get("truncated") is True
    # Query is ordered newest-first; one extraction per task/source/indicator avoids
    # attaching a recovered stale failure to a later attempt in the same run.
    seen: set[tuple[str, str, str]] = set()
    items: list[dict[str, object]] = []
    any_diagnostics = False
    issue: str | None = "invalid" if not isinstance(rows_value, list) else None
    for raw in rows:
        row = _mapping(raw)
        if row is None:
            issue = issue or "invalid"
            continue
        key = (
            str(row.get("source") or ""),
            str(row.get("task_key") or ""),
            str(row.get("indicator") or ""),
        )
        if key in seen:
            continue
        seen.add(key)
        item, recorded, item_issue = _row_projection(raw)
        any_diagnostics = any_diagnostics or recorded
        issue = issue or item_issue
        if item is not None:
            items.append(item)
    truncated_items = max(0, len(items) - MAX_REPORT_ITEMS)
    items = items[:MAX_REPORT_ITEMS]
    if rows_truncated:
        issue = "truncated"
    status = "truncated" if rows_truncated else "recorded" if any_diagnostics else (issue or "not_recorded")
    return {
        "schema_version": SCHEMA_VERSION,
        "run_id": safe_run_id or "unknown",
        "status": status,
        "items": items,
        "truncated_items": truncated_items,
        "truncated_rows": 1 if rows_truncated else 0,
    }


def unavailable_summary(run_id: str, reason: str = "not_recorded") -> dict[str, object]:
    safe_reason = reason if reason in _SUMMARY_STATUSES else "invalid"
    return {
        "schema_version": SCHEMA_VERSION,
        "run_id": _safe_id(run_id) or "unknown",
        "status": safe_reason,
        "items": [],
        "truncated_items": 0,
        "truncated_rows": 0,
    }


def sanitize_public_summary(value: object, expected_run_id: str) -> dict[str, object]:
    """Revalidate API-provided summary and bind it to the requested run."""
    mapping = _mapping(value)
    if mapping is None or mapping.get("schema_version") != SCHEMA_VERSION:
        return unavailable_summary(expected_run_id, "not_recorded")
    run_id = _safe_id(mapping.get("run_id"))
    expected = _safe_id(expected_run_id)
    if run_id is None or expected is None or run_id != expected:
        return unavailable_summary(expected_run_id, "run_mismatch")
    status_value = mapping.get("status")
    status = status_value if isinstance(status_value, str) and status_value in _SUMMARY_STATUSES else "invalid"
    raw_items = mapping.get("items")
    items_value = cast(list[object], raw_items) if isinstance(raw_items, list) else []
    items: list[dict[str, object]] = []
    for item_value in items_value[:MAX_REPORT_ITEMS]:
        raw_item = _mapping(item_value)
        if raw_item is None:
            continue
        source = raw_item.get("source")
        extraction_id = _safe_id(raw_item.get("extraction_id"))
        task_key = _safe_id(raw_item.get("task_key"))
        item_status = raw_item.get("status")
        diagnostics_status = raw_item.get("diagnostics_status")
        if (
            not isinstance(source, str) or source not in {"bcb_sgs", "b3"}
            or extraction_id is None or not isinstance(item_status, str)
            or item_status not in _EXTRACTION_STATUSES | {"unknown"}
        ):
            continue
        item: dict[str, object] = {
            "source": source,
            "extraction_id": extraction_id,
            "task_key": task_key,
            "status": item_status,
            "diagnostics_status": diagnostics_status if isinstance(diagnostics_status, str) and diagnostics_status in _SUMMARY_STATUSES else "invalid",
            "started_at": _safe_time(raw_item.get("started_at")),
            "finished_at": _safe_time(raw_item.get("finished_at")),
            "legacy_error_code": raw_item.get("legacy_error_code") if raw_item.get("legacy_error_code") is None or isinstance(raw_item.get("legacy_error_code"), str) and raw_item.get("legacy_error_code") in _LEGACY_ERROR_CODES else "unknown",
            "error_category": raw_item.get("error_category") if isinstance(raw_item.get("error_category"), str) and raw_item.get("error_category") in _CATEGORIES else None,
        }
        relation = raw_item.get("attempt_relation")
        if isinstance(relation, str) and relation in {"current", "previous", "unverified"}:
            item["attempt_relation"] = relation
        requests_raw = raw_item.get("requests")
        requests_values = cast(list[object], requests_raw) if isinstance(requests_raw, list) else []
        requests: list[dict[str, object]] = []
        for request_value in requests_values[:MAX_REQUESTS_PER_ITEM]:
            request = _mapping(request_value)
            if request is None:
                continue
            failure = request.get("failure_code")
            request_reason = request.get("validation_reason")
            if not (
                isinstance(failure, str) and failure in _DIAG_CODES
                or isinstance(request_reason, str) and request_reason in _SAFE_VALIDATION_REASONS
            ):
                continue
            response = _mapping(request.get("response")) or {}
            status_code = response.get("status_code")
            content_type = response.get("content_type")
            body_kind = response.get("body_kind")
            byte_count = response.get("bytes_received")
            safe_request: dict[str, object] = {
                "dispatch_sequence": request.get("dispatch_sequence") if type(request.get("dispatch_sequence")) is int and 1 <= cast(int, request.get("dispatch_sequence")) <= 256 else None,
                "started_at": _safe_time(request.get("started_at")),
                "finished_at": _safe_time(request.get("finished_at")),
                "duration_ms": request.get("duration_ms") if isinstance(request.get("duration_ms"), (int, float)) and not isinstance(request.get("duration_ms"), bool) and 0 <= cast(float, request.get("duration_ms")) <= 86400000 else None,
                "response": {
                    "status_code": status_code if type(status_code) is int and 100 <= status_code <= 599 else None,
                    "content_type": content_type if isinstance(content_type, str) and content_type in _MIME_TYPES else "unknown",
                    "body_kind": body_kind if isinstance(body_kind, str) and body_kind in {"unknown", "invalid_json", "null", "boolean", "object", "list", "string", "number"} else "unknown",
                    "bytes_received": byte_count if type(byte_count) is int and 0 <= byte_count <= 10 * 1024 * 1024 else None,
                },
            }
            if isinstance(failure, str) and failure in _DIAG_CODES:
                safe_request["failure_code"] = failure
            if isinstance(request_reason, str) and request_reason in _SAFE_VALIDATION_REASONS:
                safe_request["validation_reason"] = request_reason
            context = _context(request.get("request_context"))
            if context is not None:
                safe_request["request_context"] = context
            requests.append(safe_request)
        item["requests"] = requests
        item["truncated_requests"] = raw_item.get("truncated_requests") if type(raw_item.get("truncated_requests")) is int and 0 <= cast(int, raw_item.get("truncated_requests")) <= 500 else 0
        context = _context(raw_item.get("request_context"))
        if context is not None:
            item["request_context"] = context
        if raw_item.get("outcome") == "recovered":
            item["outcome"] = "recovered"
        reason = raw_item.get("validation_reason")
        if isinstance(reason, str) and reason in _SAFE_VALIDATION_REASONS:
            item["validation_reason"] = reason
        items.append(item)
    return {
        "schema_version": SCHEMA_VERSION,
        "run_id": run_id,
        "status": status,
        "items": items,
        "truncated_items": mapping.get("truncated_items") if type(mapping.get("truncated_items")) is int and 0 <= cast(int, mapping.get("truncated_items")) <= 500 else 0,
        "truncated_rows": mapping.get("truncated_rows") if type(mapping.get("truncated_rows")) is int and 0 <= cast(int, mapping.get("truncated_rows")) <= 500 else 0,
    }
