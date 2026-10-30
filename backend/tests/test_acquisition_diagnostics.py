import json

from acquisition_diagnostics import (
    MAX_TRACE_JSON_BYTES,
    SCHEMA_VERSION,
    _context,
    _json_traces,
    _safe_id,
    _safe_time,
    _trace_projection,
    sanitize_public_summary,
    summarize_refresh_run_diagnostics,
    unavailable_summary,
)


def _trace(*, failure: str | None = None, context: dict[str, object] | None = None) -> str:
    diagnostics: dict[str, object] = {
        "version": "source-request-diagnostics-v1",
        "phase": "decode",
        "duration_ms": 12.5,
        "response": {
            "status_code": 200,
            "content_type": "application/json",
            "body_kind": "object",
            "bytes_received": 11,
            "body_sha256": "a" * 64,
            "envelope_keys": [],
            "unknown_key_count": 1,
        },
    }
    if failure is not None:
        diagnostics["failure_code"] = failure
    if context is not None:
        diagnostics["request_context"] = context
    return json.dumps([{
        "dispatch_sequence": 2,
        "dispatch_started_at": "2026-10-29T00:00:00+00:00",
        "dispatch_finished_at": "2026-10-29T00:00:01+00:00",
        "diagnostics": diagnostics,
        "url": "https://private.invalid/?token=sentinel",
        "headers": {"Authorization": "sentinel"},
    }])


def test_summarizes_sgs_failure_without_raw_trace_values() -> None:
    summary = summarize_refresh_run_diagnostics("run-1", {
        "rows": [{
            "extraction_id": "extract-1", "run_id": "run-1", "source": "bcb_sgs",
            "task_key": "sgs_plan_x", "indicator": "ipca", "status": "validation_error",
            "started_at": "2026-10-29T00:00:00+00:00", "finished_at": "2026-10-29T00:00:01+00:00",
            "error_code": "InvalidSGSPayloadError", "error_detail": "expected-list",
            "error_category": "invalid_schema",
            "request_trace_json": _trace(context={
                "provider": "bcb_sgs", "series_code": 433, "requested_start": "2026-01-01",
                "requested_end": "2026-01-31", "expected_kind": "list", "local_attempt": 1,
            }),
        }], "truncated": False,
    })
    assert summary["schema_version"] == SCHEMA_VERSION
    assert summary["status"] == "recorded"
    item = summary["items"][0]
    assert item["validation_reason"] == "expected_list"
    assert item["requests"][0]["response"]["body_kind"] == "object"
    assert "sentinel" not in json.dumps(summary)


def test_missing_or_unsupported_version_is_not_upgraded() -> None:
    for diagnostics, expected in [({}, "not_recorded"), ({"version": "future"}, "unsupported_version")]:
        summary = summarize_refresh_run_diagnostics("run-1", {
            "rows": [{
                "extraction_id": "extract-1", "source": "b3", "task_key": "b3_curve",
                "indicator": "di_curve", "status": "network_error",
                "request_trace_json": json.dumps([{"diagnostics": diagnostics}]),
            }], "truncated": False,
        })
        assert summary["items"][0]["diagnostics_status"] == expected


def test_public_summary_is_rebound_to_requested_run_and_allowlisted() -> None:
    data = {
        "schema_version": SCHEMA_VERSION, "run_id": "other", "status": "recorded",
        "items": [{"source": "b3", "extraction_id": "x", "status": "network_error", "url": "sentinel"}],
    }
    assert sanitize_public_summary(data, "requested") == {
        "schema_version": SCHEMA_VERSION, "run_id": "requested", "status": "run_mismatch",
        "items": [], "truncated_items": 0, "truncated_rows": 0,
    }
    data["run_id"] = "requested"
    safe = sanitize_public_summary(data, "requested")
    assert "sentinel" not in json.dumps(safe)


def test_diagnostic_boundary_rejects_invalid_identifiers_times_contexts_and_traces() -> None:
    assert _safe_id("id:123") == "id:123"
    assert _safe_id("https://private") is None
    assert _safe_time("2026-10-29T00:00:00Z") == "2026-10-29T00:00:00+00:00"
    assert _safe_time("2026-10-29T00:00:00") is None
    assert _safe_time("bad") is None
    assert _json_traces(None) == ([], "not_recorded")
    assert _json_traces("{") == ([], "invalid")
    assert _json_traces("{}") == ([], "invalid")
    assert _context({"provider": "b3", "trade_date": "2026-10-29", "file_name": "bad.zip", "local_attempt": 1}) is None
    assert _context({"provider": "bcb_sgs", "series_code": 433, "requested_start": "2026-02-01", "requested_end": "2026-01-01", "expected_kind": "list", "local_attempt": 1}) is None
    assert _trace_projection("not-an-object")[1] == "invalid"
    assert _trace_projection({})[1] == "not_recorded"


def test_summary_caps_rows_and_public_projection_rejects_malicious_fields() -> None:
    rows: list[dict[str, object]] = []
    for index in range(12):
        rows.append({
            "extraction_id": f"extract-{index}", "source": "b3", "task_key": f"task-{index}",
            "indicator": "di_curve", "status": "network_error", "error_code": "private error text",
            "request_trace_json": _trace(failure="read_timeout"),
        })
    summary = summarize_refresh_run_diagnostics("bounded-run", {"rows": rows, "truncated": True})
    assert summary["status"] == "truncated"
    assert len(summary["items"]) == 10
    assert summary["truncated_items"] == 2
    assert summary["truncated_rows"] == 1
    assert "private error text" not in json.dumps(summary)
    assert unavailable_summary("bad/run", "unexpected") == {
        "schema_version": SCHEMA_VERSION, "run_id": "unknown", "status": "invalid",
        "items": [], "truncated_items": 0, "truncated_rows": 0,
    }


def test_unhashable_untrusted_enums_are_dropped_without_raising() -> None:
    row_summary = summarize_refresh_run_diagnostics("run-1", {
        "rows": [{
            "extraction_id": "extract-1", "source": [], "task_key": "task",
            "status": {"bad": "status"}, "error_code": ["secret"],
            "error_category": {"bad": "category"}, "request_trace_json": "[]",
        }],
    })
    assert row_summary["status"] == "invalid"
    clean = sanitize_public_summary({
        "schema_version": SCHEMA_VERSION,
        "run_id": "run-1",
        "status": [],
        "items": [{
            "source": "b3", "extraction_id": "extract-1", "task_key": "task",
            "status": "network_error", "diagnostics_status": [],
            "legacy_error_code": {}, "error_category": [],
            "requests": [{
                "failure_code": [], "validation_reason": [],
                "request_context": {"provider": [], "local_attempt": {}},
            }],
        }],
    }, "run-1")
    assert clean["status"] == "invalid"
    assert clean["items"][0]["diagnostics_status"] == "invalid"
    assert clean["items"][0]["legacy_error_code"] == "unknown"
    assert clean["items"][0]["error_category"] is None
    assert clean["items"][0]["requests"] == []


def test_unrecognized_sgs_validation_detail_is_never_reflected() -> None:
    secret = "password=JWT_SENTINEL"
    summary = summarize_refresh_run_diagnostics("run-1", {
        "rows": [{
            "extraction_id": "extract-1", "source": "bcb_sgs", "task_key": "sgs_juros",
            "indicator": "433", "status": "validation_error",
            "error_code": "InvalidSGSPayloadError", "error_detail": secret,
            "request_trace_json": _trace(),
        }],
    })
    item = summary["items"][0]
    assert item["validation_reason"] == "unknown"
    assert item["diagnostics_status"] == "recorded"
    assert secret not in json.dumps(summary)


def test_reader_enforces_full_trace_blob_and_entry_count_bounds() -> None:
    assert _json_traces("x" * (MAX_TRACE_JSON_BYTES + 1)) == ([], "invalid")
    many = json.dumps([{} for _ in range(300)])
    entries, status = _json_traces(many)
    assert status is None
    assert len(entries) == 256
