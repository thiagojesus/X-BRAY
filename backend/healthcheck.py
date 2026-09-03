import argparse
import json
import os
import sys
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Literal, NotRequired, TypeAlias, TypedDict
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen


JsonScalar: TypeAlias = str | int | float | bool | None
JsonValue: TypeAlias = JsonScalar | list["JsonValue"] | dict[str, "JsonValue"]
JsonObject: TypeAlias = dict[str, JsonValue]


class PayloadValidationError(ValueError):
    pass


class HealthcheckProbeError(RuntimeError):
    pass


class RefreshSuccess(TypedDict):
    url: str
    http_status: int
    latency_seconds: float
    status: Literal["ok"]
    tasks: list[JsonObject]


class RefreshFailure(TypedDict):
    status: Literal["error"]
    error: str


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
    EndpointSpec("/api/cambio", (("data",),)),
    EndpointSpec("/api/titulos", (("data",),)),
    EndpointSpec("/api/focus", (("data",),)),
    EndpointSpec("/api/complementares", (("data",),)),
    EndpointSpec("/api/tesouro-direto", (("data", "prefixado"), ("data", "ipca"))),
    EndpointSpec("/api/curvas-di", (("dates",), ("curves",))),
    EndpointSpec(
        "/api/eleicoes/estados",
        (("national", "candidates"), ("national", "history"), ("days",), ("ufs",)),
    ),
)


def _resolve(payload: JsonValue, path: tuple[str, ...]) -> JsonValue:
    current = payload
    for key in path:
        if not isinstance(current, dict) or key not in current:
            raise PayloadValidationError(f"missing JSON path: {'.'.join(path)}")
        current = current[key]
    return current


def _has_content(value: JsonValue) -> bool:
    if isinstance(value, list):
        return bool(value)
    if isinstance(value, dict):
        return bool(value) and all(_has_content(child) for child in value.values())
    if isinstance(value, str):
        return bool(value.strip())
    return value is not None


def validate_payload(payload: JsonValue, required_paths: tuple[tuple[str, ...], ...]) -> None:
    for path in required_paths:
        if not _has_content(_resolve(payload, path)):
            raise PayloadValidationError(f"empty JSON path: {'.'.join(path)}")


def freshness_hours(value: JsonValue) -> float:
    if not isinstance(value, str) or not value:
        raise PayloadValidationError("last_updated is missing")
    updated = datetime.fromisoformat(value.replace("Z", "+00:00"))
    now = datetime.now(updated.tzinfo) if updated.tzinfo else datetime.now()
    return max(0.0, (now - updated).total_seconds() / 3600)


def request_json(url: str, method: str, timeout: float) -> tuple[int, JsonValue, float]:
    request = Request(url, data=b"" if method == "POST" else None, method=method)
    started = time.perf_counter()
    try:
        with urlopen(request, timeout=timeout) as response:
            status = response.status
            body = response.read()
    except HTTPError as exc:
        status = exc.code
        body = exc.read()
    elapsed = time.perf_counter() - started
    try:
        payload: JsonValue = json.loads(body.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise HealthcheckProbeError(f"invalid JSON from {url}: {exc}") from exc
    return status, payload, elapsed


def check_refresh(base_url: str) -> RefreshSuccess:
    url = f"{base_url}/api/refresh?force=true"
    status, payload, elapsed = request_json(url, "POST", 300)
    if status != 200:
        raise HealthcheckProbeError(f"refresh returned HTTP {status}")
    if not isinstance(payload, dict) or payload.get("status") != "ok":
        raise HealthcheckProbeError(f"refresh failed: {payload}")
    raw_tasks = payload.get("tasks")
    if not isinstance(raw_tasks, list) or len(raw_tasks) != 14:
        raise HealthcheckProbeError("refresh did not report all 14 tasks")
    tasks: list[JsonObject] = []
    for task in raw_tasks:
        if not isinstance(task, dict) or task.get("status") != "ok":
            raise HealthcheckProbeError(f"refresh task failed: {raw_tasks}")
        tasks.append(task)
    return {
        "url": url,
        "http_status": status,
        "latency_seconds": round(elapsed, 3),
        "status": "ok",
        "tasks": tasks,
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
            age = freshness_hours(_resolve(payload, ("last_updated",)))
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
            refresh_result = {"status": "error", "error": str(exc)}
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

    report = run(args.base_url, args.refresh, args.max_latency, args.max_age_hours)
    Path(args.output).write_text(json.dumps(report, ensure_ascii=True, indent=2) + "\n", encoding="utf-8")
    print(json.dumps(report, ensure_ascii=True, indent=2))
    return 0 if report["status"] == "ok" else 1


if __name__ == "__main__":
    sys.exit(main())
