"""GitHub Actions entry point; only the report files are written locally."""

from __future__ import annotations

import json
import os
from pathlib import Path
from typing import cast
from urllib.parse import urlparse

import httpx

from source_audit.auditor import render_markdown, run_audit

REPORT_JSON = Path("source-audit-report.json")
REPORT_MARKDOWN = Path("source-audit-report.md")
SNAPSHOT_PATH = Path(os.environ.get("SOURCE_AUDIT_SNAPSHOT_FILE", "")) if os.environ.get("SOURCE_AUDIT_SNAPSHOT_FILE") else None
EXPECTED_PUBLIC_HOSTS = frozenset({"xbray-api.vercel.app"})


def _safe_public_host(value: str | None) -> str | None:
    if not value:
        return None
    host = value.lower().rstrip(".")
    return host if host in EXPECTED_PUBLIC_HOSTS else None


def _observed_request_host(value: object) -> tuple[str | None, bool]:
    if not isinstance(value, dict):
        return None, False
    metadata = cast(dict[object, object], value)
    host_value = metadata.get("request_host")
    host = _safe_public_host(host_value) if isinstance(host_value, str) else None
    matches = metadata.get("matches_expected_production_host") is True and host == "xbray-api.vercel.app"
    return host, matches


def _snapshot() -> dict[str, object]:
    if SNAPSHOT_PATH is not None:
        file_payload: object = json.loads(SNAPSHOT_PATH.read_text(encoding="utf-8"))
        return _validate_snapshot(file_payload)
    api_url = os.environ.get("XBRAY_API_URL", "").strip().rstrip("/")
    token = os.environ.get("XBRAY_REFRESH_TOKEN", "").strip()
    parsed = urlparse(api_url)
    if parsed.scheme != "https" or not parsed.hostname or not token:
        raise RuntimeError("source_audit_configuration_missing")
    with httpx.Client(timeout=httpx.Timeout(90, connect=10), follow_redirects=False) as client:
        response = client.get(
            f"{api_url}/api/source-audit/snapshot",
            headers={"Authorization": f"Bearer {token}", "Accept": "application/json"},
        )
        response.raise_for_status()
        api_payload: object = response.json()
    snapshot = _validate_snapshot(api_payload)
    configured_host = _safe_public_host(parsed.hostname)
    response_url = getattr(response, "url", None)
    response_host = _safe_public_host(getattr(response_url, "host", None))
    observed_host, observed_matches = _observed_request_host(snapshot.get("request_provenance"))
    history = getattr(response, "history", ())
    redirect_hosts = [
        host for item in history
        if (host := _safe_public_host(getattr(getattr(item, "url", None), "host", None))) is not None
    ]
    snapshot["request_provenance"] = {
        "configured_host": configured_host,
        "effective_request_host": configured_host,
        "response_host": response_host,
        "matches_expected_production_host": configured_host == "xbray-api.vercel.app",
        "api_observed_host": observed_host,
        "api_observed_matches_expected_host": observed_matches,
        "redirect_count": len(history),
        "allowlisted_redirect_hosts": redirect_hosts,
        "unrecognized_redirect_host_count": len(history) - len(redirect_hosts),
    }
    return snapshot


def _validate_snapshot(payload: object) -> dict[str, object]:
    if not isinstance(payload, dict):
        raise TypeError("invalid_source_audit_snapshot")
    entries = cast(dict[object, object], payload)
    if entries.get("schema_version") != "source-audit-snapshot-v1":
        raise TypeError("invalid_source_audit_snapshot")
    normalized: dict[str, object] = {}
    for key, value in entries.items():
        if not isinstance(key, str):
            raise TypeError("invalid_source_audit_snapshot")
        normalized[key] = value
    return normalized


def main() -> int:
    report: dict[str, object]
    try:
        report = run_audit(_snapshot())
    except Exception as exc:  # noqa: BLE001 - always emit a degraded report for workflow diagnostics.
        # Always leave a machine-readable failure for artifact/issue handling.
        report = {
            "schema_version": "source-audit-report-v2",
            "generated_at": __import__("datetime").datetime.now(__import__("datetime").timezone.utc).isoformat(),
            "status": "degraded",
            "coverage": {"expected_static": 75, "checked": 0, "complete": False},
            "counts_by_status": {"source_unavailable": 1},
            "error_code": type(exc).__name__,
            "sources": {},
        }
    REPORT_JSON.write_text(json.dumps(report, ensure_ascii=False, sort_keys=True, indent=2) + "\n", encoding="utf-8")
    REPORT_MARKDOWN.write_text(render_markdown(report), encoding="utf-8")
    summary = os.environ.get("GITHUB_STEP_SUMMARY")
    if summary:
        with open(summary, "a", encoding="utf-8") as stream:
            stream.write(render_markdown(report))
    return 0 if report.get("status") == "healthy" else 1


if __name__ == "__main__":
    raise SystemExit(main())
