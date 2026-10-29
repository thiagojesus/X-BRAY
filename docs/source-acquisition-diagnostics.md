# Source acquisition diagnostics

Source-request diagnostics are additive evidence about bounded provider requests. They do not change acquisition policy, retry eligibility, circuit behavior, freshness, data quality, or the business-data contract.

## Evidence and versions

- A persisted `source_extractions.request_trace_json` may contain request records with `diagnostics.version = "source-request-diagnostics-v1"`. Each diagnostic records only allowlisted evidence such as the observed HTTP status, content type, body kind, received byte count, bounded duration, a safe exception code, and validated SGS/B3 request context. Raw response bytes, response values, request URLs, headers, cookies, and exception messages are not recorded in the diagnostic object.
- The read-only `GET /api/refresh-status` adds `acquisition_diagnostics` with `schema_version = "acquisition-diagnostics-v1"`, the selected refresh `run_id`, availability `status`, bounded `items`, `truncated_items`, and `truncated_rows`. It reads only bounded extraction rows for that run and repeats strict allowlist validation before returning them. It does not expose the stored JSON wholesale.
- Missing historical fields remain `not_recorded`; malformed traces remain `invalid`; versions other than the exact supported marker are `unsupported_version`. No historical trace is reconstructed or backfilled.

An HTTP `200` means the provider returned an HTTP response, not that its payload passed source validation. For example, an observed HTTP `200` JSON object with 11 bytes may still fail an SGS list contract. When the persisted extraction contains the allowlisted validation reason, the summary can report the observed response shape and `expected_list`; it does not invent a transport failure code or a validated receipt. A valid JSON list rejected for a value problem is reported as `invalid_value`, not `expected_list`. Older rows without the typed trace cannot establish their historical body kind or request cause.

Diagnostic codes describe the best bounded observed evidence, not an asserted physical root cause. An outer legacy `RuntimeError` can coexist with an inner observed `read_timeout`; the diagnostic does not rewrite the extraction's operational status or category. Unknown exception/detail values remain `unknown`. Diagnostic classification never authorizes retries, circuit changes, or source-operation decisions.

## Correlation and time

- SGS request context includes series code and requested ISO date window. B3 context includes trade date, expected `SPRDyymmdd.zip` filename, and local download attempt.
- `local_attempt` is the provider wrapper's local dispatch attempt; it is distinct from a refresh item's `attempt`, extraction `retry_count`, and central retry-budget counters. The HTTP trace list is append order (`dispatch_sequence`), not a claim that requests completed or started in that same order.
- Persisted timestamps are timezone-aware UTC instants. To display a local time, convert the UTC instant to `America/Sao_Paulo`; do not infer a provider publication time or apply a fixed offset to a naive timestamp.
- Current/previous/unverified attempt labels are based on the persisted work-item claim generation and attempt identity when available. A prior failure is not presented as the current generation's cause.

## Operator access and limits

Use the safe, bounded summary at `GET /api/refresh-status` for routine run triage. The full bounded serialized trace remains in `source_extractions.request_trace_json` for authorized operational database access; this change adds no HTTP route for raw traces. The existing authenticated `GET /api/source-audit/snapshot` remains a separate protected source-data snapshot, and the provider `/probe` endpoint is not part of diagnostics reporting. Do not expose database credentials or claim tokens when retrieving traces.

The healthcheck JSON artifact (default `health-report.json`, or the configured `--output`) carries the same sanitized `acquisition-diagnostics-v1` structure when a refresh status response is available. Partial refreshes remain failures with their legacy failure text even if status and page probes return HTTP `200`. Older servers without the field are reported as `not_recorded`; a run-ID mismatch is labeled `run_mismatch` and is never attached to the requested run.

The reader bounds a run query at 500 rows (with a 501st-row truncation check), public summaries at 10 items, and request details at three requests per item. The stored trace JSON and each diagnostic object are also size/count bounded. Truncation and unknown evidence are explicit; they are not success signals.

## Synthetic example

```json
{
  "schema_version": "acquisition-diagnostics-v1",
  "run_id": "example-run",
  "status": "recorded",
  "items": [{
    "source": "bcb_sgs",
    "status": "validation_error",
    "diagnostics_status": "recorded",
    "validation_reason": "expected_list",
    "requests": [{
      "response": {
        "status_code": 200,
        "content_type": "application/json",
        "body_kind": "object",
        "bytes_received": 11
      },
      "validation_reason": "expected_list",
      "request_context": {
        "provider": "bcb_sgs",
        "series_code": 433,
        "requested_start": "2026-01-01",
        "requested_end": "2026-01-31",
        "expected_kind": "list",
        "local_attempt": 1
      }
    }]
  }],
  "truncated_items": 0,
  "truncated_rows": 0
}
```

This is a synthetic schema illustration, not an assertion about any production incident or historical row.
