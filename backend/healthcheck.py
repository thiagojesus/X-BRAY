import argparse
import json
import os
import sys
import time
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import datetime, timezone
from http.client import RemoteDisconnected
from pathlib import Path
from typing import Literal, NotRequired, TypeAlias, TypedDict, cast
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen
from uuid import uuid4

from healthcheck_payloads import (
    JsonObject,
    JsonValue,
    PayloadValidationError,
    resolve_json_path,
    validate_payload,
)
from refresh_run_client import RefreshRunClientConfig, run_resumable_refresh
from acquisition_diagnostics import sanitize_public_summary, unavailable_summary


class HealthcheckProbeError(RuntimeError):
    def __init__(self, message: str, acquisition_diagnostics: JsonObject | None = None) -> None:
        self.acquisition_diagnostics = acquisition_diagnostics
        super().__init__(message)


class RefreshSuccess(TypedDict):
    url: str
    http_status: int
    latency_seconds: float
    status: Literal["ok"]
    tasks: list[JsonObject]
    refresh_status: JsonObject
    acquisition_diagnostics: NotRequired[JsonObject]


class RefreshFailure(TypedDict):
    status: Literal["error"]
    error: str
    acquisition_diagnostics: NotRequired[JsonObject]


RefreshResult: TypeAlias = RefreshSuccess | RefreshFailure


class EndpointSuccess(TypedDict):
    path: str
    status: Literal["ok"]
    http_status: int
    latency_seconds: float
    freshness_hours: NotRequired[float]


class EndpointFailure(TypedDict):
    path: str
    status: Literal["error"]
    latency_seconds: float
    error: str


EndpointResult: TypeAlias = EndpointSuccess | EndpointFailure


class HealthReport(TypedDict):
    generated_at: str
    base_url: str
    status: Literal["ok", "error"]
    refresh: RefreshResult | None
    endpoints: list[EndpointResult]
    failures: list[str]


@dataclass(frozen=True, slots=True)
class EndpointSpec:
    path: str
    required_paths: tuple[tuple[str, ...], ...]


ENDPOINTS = (
    EndpointSpec("/api/status", (("last_updated",),)),
    EndpointSpec("/api/juros", (("data",),)),
    EndpointSpec("/api/inflacao", (("data",),)),
    EndpointSpec(
        "/api/ipca-decomposicao/tudo",
        (("grupos",), ("naturezas",), ("core",), ("precos",)),
    ),
    EndpointSpec("/api/atividade", (("data",),)),
    EndpointSpec("/api/atividade/sidra", (("data",), ("metadata",))),
    EndpointSpec("/api/cambio", (("data",),)),
    EndpointSpec("/api/titulos", (("data",),)),
    EndpointSpec("/api/focus", (("data",),)),
    EndpointSpec("/api/complementares", (("data",),)),
    EndpointSpec("/api/tesouro-direto", (("data", "prefixado"), ("data", "ipca"))),
    EndpointSpec("/api/curvas-di", (("dates",), ("curves",))),
    EndpointSpec("/api/fiscal", (("data",), ("metadata",))),
    EndpointSpec("/api/credito", (("data",), ("metadata",))),
)


def freshness_hours(value: JsonValue) -> float:
    if not isinstance(value, str) or not value:
        raise PayloadValidationError("last_updated is missing")
    updated = datetime.fromisoformat(value.replace("Z", "+00:00"))
    now = datetime.now(updated.tzinfo)
    return max(0.0, (now - updated).total_seconds() / 3600)


def request_json(
    url: str,
    method: str,
    timeout: float,
    headers: Mapping[str, str] | None = None,
    body: JsonValue | bytes | None = None,
) -> tuple[int, JsonValue, float]:
    request_headers = dict(headers or {})
    if body is not None:
        request_headers["Content-Type"] = "application/json"
    data = json.dumps(body).encode("utf-8") if body is not None else (b"" if method == "POST" else None)
    request = Request(
        url,
        data=data,
        method=method,
        headers=request_headers,
    )
    started = time.perf_counter()
    response_body: bytes
    try:
        with urlopen(request, timeout=timeout) as response:
            status = response.status
            response_body = response.read()
    except HTTPError as exc:
        status = exc.code
        response_body = exc.read()
    except (RemoteDisconnected, ConnectionResetError, ConnectionAbortedError, BrokenPipeError) as exc:
        raise HealthcheckProbeError("remote connection ended without an HTTP response") from exc
    elapsed = time.perf_counter() - started
    try:
        payload: JsonValue = json.loads(response_body.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise HealthcheckProbeError(f"invalid JSON from {url}: {exc}") from exc
    return status, payload, elapsed


def check_refresh(base_url: str) -> RefreshSuccess:
    url = f"{base_url}/api/refresh-runs"
    token = os.environ.get("XBRAY_REFRESH_TOKEN", "").strip()
    if not token:
        raise HealthcheckProbeError("XBRAY_REFRESH_TOKEN is required for refresh")
    configured_run_id = os.environ.get("XBRAY_REFRESH_RUN_ID", "").strip()
    resume_existing = os.environ.get("XBRAY_RESUME_EXISTING", "").strip().lower() == "true"
    if resume_existing and not configured_run_id:
        raise HealthcheckProbeError("XBRAY_REFRESH_RUN_ID is required to resume an existing run")
    run_id = configured_run_id or uuid4().hex
    try:
        refresh_result = run_resumable_refresh(
            RefreshRunClientConfig(
                base_url=base_url,
                run_id=run_id,
                cycle_id=run_id,
                token=token,
                resume_existing=resume_existing,
            ),
            request_json,
        )
    except (RuntimeError, URLError, TimeoutError) as exc:
        diagnostic = cast(JsonObject, unavailable_summary(run_id))
        raise HealthcheckProbeError(str(exc), diagnostic) from exc
    health_url = f"{base_url}/api/refresh-status"
    try:
        health_http_status, health_payload, _ = request_json(health_url, "GET", 30)
    except (RuntimeError, URLError, TimeoutError) as exc:
        diagnostic = cast(JsonObject, unavailable_summary(run_id))
        raise HealthcheckProbeError(str(exc), diagnostic) from exc
    if health_http_status != 200 or not isinstance(health_payload, dict):
        diagnostic = cast(JsonObject, unavailable_summary(run_id))
        raise HealthcheckProbeError(f"refresh status returned HTTP {health_http_status}", diagnostic)
    acquisition_diagnostics = sanitize_public_summary(
        health_payload.get("acquisition_diagnostics"), run_id
    )
    typed_diagnostics = cast(JsonObject, acquisition_diagnostics)
    overall_status = health_payload.get("overall_status")
    health_tasks = health_payload.get("tasks")
    if overall_status not in {"ok", "partial"} or not isinstance(health_tasks, list):
        raise HealthcheckProbeError("refresh status reports no usable task", typed_diagnostics)
    usable = any(
        isinstance(task, dict)
        and task.get("last_success")
        and task.get("freshness") in {"ok", "stale"}
        for task in health_tasks
    )
    if not usable:
        raise HealthcheckProbeError("refresh status reports no usable task", typed_diagnostics)
    if refresh_result.status != "ok":
        failed_tasks = [
            f"{task.get('name') or 'unnamed task'} ({task.get('error_code') or task.get('status') or 'incomplete'})"
            for task in refresh_result.tasks
            if task.get("status") != "ok"
        ]
        detail = "; ".join(failed_tasks) or "one or more tasks incomplete"
        raise HealthcheckProbeError(f"refresh run completed partially: {detail}", typed_diagnostics)
    refresh_status: JsonObject = dict(health_payload)
    refresh_status["acquisition_diagnostics"] = typed_diagnostics
    return {
        "url": url,
        "http_status": refresh_result.http_status,
        "latency_seconds": round(refresh_result.latency_seconds, 3),
        "status": refresh_result.status,
        "tasks": refresh_result.tasks,
        "refresh_status": refresh_status,
        "acquisition_diagnostics": typed_diagnostics,
    }


def check_endpoint(
    base_url: str,
    spec: EndpointSpec,
    max_latency: float,
    max_age_hours: float,
) -> EndpointResult:
    url = f"{base_url}{spec.path}"
    started = time.perf_counter()
    try:
        status, payload, elapsed = request_json(url, "GET", 30)
        if status != 200:
            raise HealthcheckProbeError(f"HTTP {status}: {payload}")
        if elapsed > max_latency:
            raise HealthcheckProbeError(f"latency {elapsed:.3f}s exceeded {max_latency:.3f}s")
        validate_payload(payload, spec.required_paths)
        result: EndpointSuccess = {
            "path": spec.path,
            "status": "ok",
            "http_status": status,
            "latency_seconds": round(elapsed, 3),
        }
        if spec.path == "/api/status":
            age = freshness_hours(resolve_json_path(payload, ("last_updated",)))
            result["freshness_hours"] = round(age, 3)
            if age > max_age_hours:
                raise HealthcheckProbeError(
                    f"last_updated age {age:.3f}h exceeded {max_age_hours:.3f}h"
                )
        return result
    except (RuntimeError, ValueError, URLError, TimeoutError) as exc:
        return {
            "path": spec.path,
            "status": "error",
            "latency_seconds": round(time.perf_counter() - started, 3),
            "error": str(exc),
        }


def run(base_url: str, refresh: bool, max_latency: float, max_age_hours: float) -> HealthReport:
    normalized_url = base_url.rstrip("/")
    refresh_result: RefreshResult | None = None
    failures: list[str] = []

    valid_url = normalized_url.startswith(("http://", "https://"))
    if not valid_url:
        failures.append("XBRAY_API_URL must be an absolute HTTP(S) URL")
    elif refresh:
        try:
            refresh_result = check_refresh(normalized_url)
        except (RuntimeError, URLError, TimeoutError) as exc:
            diagnostic = getattr(exc, "acquisition_diagnostics", None)
            requested_run_id = os.environ.get("XBRAY_REFRESH_RUN_ID", "").strip()
            if not requested_run_id and isinstance(diagnostic, Mapping):
                safe_mapping = cast(Mapping[str, object], diagnostic)
                candidate = safe_mapping.get("run_id")
                requested_run_id = candidate if isinstance(candidate, str) else "unknown"
            requested_run_id = requested_run_id or "unknown"
            safe_diagnostic = sanitize_public_summary(cast(object, diagnostic), requested_run_id)
            refresh_result = {
                "status": "error", "error": str(exc),
                "acquisition_diagnostics": cast(JsonObject, safe_diagnostic),
            }
            failures.append(str(exc))

    endpoint_results: list[EndpointResult] = []
    if valid_url:
        endpoint_results = [
            check_endpoint(normalized_url, spec, max_latency, max_age_hours)
            for spec in ENDPOINTS
        ]
        failures.extend(
            f"{result['path']}: {result['error']}"
            for result in endpoint_results
            if result["status"] == "error"
        )

    report_status: Literal["ok", "error"] = "error" if failures else "ok"
    return {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "base_url": normalized_url,
        "status": report_status,
        "refresh": refresh_result,
        "endpoints": endpoint_results,
        "failures": failures,
    }


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--base-url", default=os.environ.get("XBRAY_API_URL", ""))
    parser.add_argument("--output", default="health-report.json")
    parser.add_argument("--refresh", action="store_true")
    parser.add_argument("--max-latency", type=float, default=float(os.environ.get("XBRAY_MAX_LATENCY_SECONDS", "5")))
    parser.add_argument("--max-age-hours", type=float, default=float(os.environ.get("XBRAY_MAX_AGE_HOURS", "30")))
    args = parser.parse_args()

    try:
        report = run(args.base_url, args.refresh, args.max_latency, args.max_age_hours)
    except Exception as exc:  # noqa: BLE001 - preserve a failure artifact for any ordinary runtime error
        # Keep CI's artifact and incident-sync steps operational without
        # including exception text that could contain request URLs or secrets.
        message = f"healthcheck terminated unexpectedly ({type(exc).__name__})"
        print(message, file=sys.stderr)
        report = {
            "generated_at": datetime.now(timezone.utc).isoformat(),
            "base_url": args.base_url.rstrip("/"),
            "status": "error",
            "refresh": {"status": "error", "error": message} if args.refresh else None,
            "endpoints": [],
            "failures": [message],
        }
    Path(args.output).write_text(json.dumps(report, ensure_ascii=True, indent=2) + "\n", encoding="utf-8")
    print(json.dumps(report, ensure_ascii=True, indent=2))
    return 0 if report["status"] == "ok" else 1


if __name__ == "__main__":
    sys.exit(main())
