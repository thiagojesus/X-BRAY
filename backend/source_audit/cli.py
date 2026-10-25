"""GitHub Actions entry point; only the report files are written locally."""

from __future__ import annotations

import json
import os
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import cast
from urllib.parse import urlencode, urlparse

import httpx

from source_audit.auditor import collect_provider_probe, render_markdown, run_audit

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
    if (
        parsed.scheme != "https"
        or _safe_public_host(parsed.hostname) is None
        or parsed.path not in ("", "/")
        or parsed.query
        or parsed.fragment
        or parsed.username is not None
        or parsed.password is not None
        or not token
    ):
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


def _application_probe(provider: str, checked_at: str) -> dict[str, object]:
    api_url = os.environ.get("XBRAY_API_URL", "").strip().rstrip("/")
    token = os.environ.get("XBRAY_REFRESH_TOKEN", "").strip()
    parsed = urlparse(api_url)
    if (
        parsed.scheme != "https" or _safe_public_host(parsed.hostname) is None
        or parsed.path not in ("", "/") or parsed.query or parsed.fragment or not token
    ):
        return {
            "provider": provider, "environment": "application", "status": "invalid_source",
            "failure_class": "environment_configuration", "error_code": "application_probe_configuration_invalid",
        }
    target = f"{api_url}/api/source-audit/probe?{urlencode({'provider': provider, 'checked_at': checked_at})}"
    try:
        with (
            httpx.Client(timeout=httpx.Timeout(95, connect=10), follow_redirects=False) as client,
            client.stream(
                "GET", target,
                headers={"Authorization": f"Bearer {token}", "Accept": "application/json"},
            ) as response,
        ):
            response.raise_for_status()
            body = bytearray()
            for chunk in response.iter_bytes(chunk_size=64 * 1024):
                if len(body) + len(chunk) > 2 * 1024 * 1024:
                    return {
                        "provider": provider, "environment": "application", "status": "invalid_source",
                        "failure_class": "source_contract", "error_code": "application_probe_response_too_large",
                    }
                body.extend(chunk)
            payload: object = json.loads(body)
        if not isinstance(payload, dict):
            raise TypeError("application_probe_response_invalid")
        payload_row = cast(dict[object, object], payload)
        if payload_row.get("provider") != provider or payload_row.get("environment") != "application":
            raise TypeError("application_probe_response_invalid")
        if payload_row.get("status") not in {"valid", "invalid_source", "source_unavailable"}:
            raise TypeError("application_probe_response_invalid")
        payload_checked_at = payload_row.get("cutoff_at")
        if payload_checked_at != checked_at:
            raise TypeError("application_probe_cutoff_mismatch")
        return cast(dict[str, object], payload)
    except httpx.HTTPStatusError as exc:
        response = exc.response
        retry_after = response.headers.get("retry-after")
        result: dict[str, object] = {
            "provider": provider, "environment": "application", "status": "source_unavailable",
            "failure_class": "transport", "error_code": "application_probe_http_error",
            "http_status": response.status_code,
        }
        if retry_after and retry_after.isdigit():
            result["retry_after_seconds"] = int(retry_after)
        return result
    except (httpx.TimeoutException, httpx.NetworkError, TimeoutError, OSError) as exc:
        return {
            "provider": provider, "environment": "application", "status": "source_unavailable",
            "failure_class": "transport", "error_code": type(exc).__name__,
        }
    except (TypeError, ValueError, json.JSONDecodeError) as exc:
        return {
            "provider": provider, "environment": "application", "status": "invalid_source",
            "failure_class": "source_contract", "error_code": type(exc).__name__,
        }


def _dual_environment_probes(snapshot: dict[str, object]) -> dict[str, dict[str, dict[str, object]]]:
    checked_at = snapshot.get("captured_at")
    if not isinstance(checked_at, str):
        checked_at = datetime.now(timezone.utc).isoformat()
    github_b3 = collect_provider_probe("b3", checked_at, environment="github_actions")
    b3_completed = time.monotonic()
    github_treasury = collect_provider_probe("tesouro", checked_at, environment="github_actions")
    remaining = 30 - (time.monotonic() - b3_completed)
    if remaining > 0:
        time.sleep(remaining)
    application_b3 = _application_probe("b3", checked_at)
    application_treasury = _application_probe("tesouro", checked_at)
    return {
        "b3": {"github_actions": github_b3, "application": application_b3},
        "tesouro": {"github_actions": github_treasury, "application": application_treasury},
    }


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
        snapshot = _snapshot()
        if SNAPSHOT_PATH is None:
            report = run_audit(snapshot, provider_environment_probes=_dual_environment_probes(snapshot))
        else:
            report = run_audit(snapshot)
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
