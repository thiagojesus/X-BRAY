import json
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Literal, Protocol, TypeAlias
from urllib.error import URLError

from pydantic import BaseModel, ConfigDict, ValidationError

from db.refresh_run_models import MAX_WORK_ITEM_ATTEMPTS
from healthcheck_payloads import JsonObject, JsonValue

RunStatus: TypeAlias = Literal["running", "succeeded", "partial", "failed"]
ItemStatus: TypeAlias = Literal["pending", "claimed", "succeeded", "partial", "failed", "skipped"]
RefreshStatus: TypeAlias = Literal["ok", "partial", "error"]
RefreshTaskStatus: TypeAlias = Literal["ok", "partial", "error", "skipped"]


class RequestJson(Protocol):
    def __call__(
        self,
        url: str,
        method: str,
        timeout: float,
        headers: dict[str, str] | None = None,
        body: JsonValue | None = None,
    ) -> tuple[int, JsonValue, float]: ...


class RefreshRunClientError(RuntimeError):
    def __init__(self, code: str) -> None:
        self.code = code
        super().__init__(code)


class _Run(BaseModel):
    model_config = ConfigDict(frozen=True, extra="ignore")

    run_id: str
    cycle_id: str
    state: RunStatus
    force: bool
    manifest_version: str
    final_result: str | None


class _WorkItem(BaseModel):
    model_config = ConfigDict(frozen=True, extra="ignore")

    item_key: str
    status: ItemStatus
    attempt: int
    claim_expires_at: str | None


class _Snapshot(BaseModel):
    model_config = ConfigDict(frozen=True, extra="ignore")

    run: _Run
    items: tuple[_WorkItem, ...]


class _FinalTask(BaseModel):
    model_config = ConfigDict(frozen=True, extra="ignore")

    name: str
    status: RefreshTaskStatus
    error_code: str | None


class _FinalResult(BaseModel):
    model_config = ConfigDict(frozen=True, extra="ignore")

    status: RefreshStatus
    tasks: tuple[_FinalTask, ...]


@dataclass(frozen=True, slots=True)
class RefreshRunClientConfig:
    base_url: str
    run_id: str
    cycle_id: str
    token: str
    force: bool = False
    manifest_version: str = "daily-refresh-v2"
    compatible_manifest_versions: tuple[str, ...] = ("daily-refresh-v1",)
    max_wait_seconds: int = 7200
    resume_existing: bool = False


@dataclass(frozen=True, slots=True)
class RefreshRunClientResult:
    status: Literal["ok", "partial"]
    tasks: list[JsonObject]
    http_status: int
    latency_seconds: float


def _snapshot(config: RefreshRunClientConfig, payload: JsonValue) -> _Snapshot:
    try:
        snapshot = _Snapshot.model_validate(payload)
    except ValidationError as exc:
        raise RefreshRunClientError("refresh run returned an invalid status document") from exc
    if (
        snapshot.run.run_id != config.run_id
        or snapshot.run.cycle_id != config.cycle_id
        or snapshot.run.force != config.force
        or snapshot.run.manifest_version
        not in (config.manifest_version, *config.compatible_manifest_versions)
    ):
        raise RefreshRunClientError("refresh run status disagrees with the stable workflow identity")
    return snapshot


def _read_status(config: RefreshRunClientConfig, request_json: RequestJson) -> _Snapshot | None:
    url = f"{config.base_url}/api/refresh-runs/{config.run_id}"
    headers = {"Authorization": f"Bearer {config.token}"}
    for attempt in range(3):
        try:
            status, payload, _ = request_json(url, "GET", 30, headers)
        except (RuntimeError, URLError, TimeoutError):
            if attempt == 2:
                break
            time.sleep(attempt + 1)
            continue
        if status == 404:
            return None
        if status == 200:
            return _snapshot(config, payload)
        if status in (401, 403, 409, 422, 503):
            raise RefreshRunClientError(f"refresh run status returned HTTP {status}")
        if attempt < 2:
            time.sleep(attempt + 1)
    raise RefreshRunClientError("refresh run status could not reconcile the request")


def _start_run(config: RefreshRunClientConfig, request_json: RequestJson) -> _Snapshot:
    url = f"{config.base_url}/api/refresh-runs"
    headers = {"Authorization": f"Bearer {config.token}"}
    if config.resume_existing:
        snapshot = _read_status(config, request_json)
        if snapshot is None:
            raise RefreshRunClientError("requested refresh run does not exist; refusing to create a new run")
    body: JsonObject = {
        "run_id": config.run_id,
        "cycle_id": config.cycle_id,
        "force": config.force,
    }
    for attempt in range(2):
        try:
            status, payload, _ = request_json(url, "POST", 30, headers, body)
        except (RuntimeError, URLError, TimeoutError):
            snapshot = _read_status(config, request_json)
            if snapshot is not None:
                return snapshot
            if attempt == 1:
                break
            continue
        if status == 200:
            return _snapshot(config, payload)
        if status in (401, 403, 409, 422, 503):
            if status == 409:
                snapshot = _read_status(config, request_json)
                if snapshot is not None:
                    return snapshot
            raise RefreshRunClientError(f"refresh run start returned HTTP {status}")
        snapshot = _read_status(config, request_json)
        if snapshot is not None:
            return snapshot
        if attempt == 1:
            break
    raise RefreshRunClientError("refresh run start outcome could not be reconciled")


def _post_item(
    config: RefreshRunClientConfig,
    request_json: RequestJson,
    item_key: str,
) -> _Snapshot | None:
    url = f"{config.base_url}/api/refresh-runs/{config.run_id}/advance"
    headers = {"Authorization": f"Bearer {config.token}"}
    body: JsonObject = {"expected_item_key": item_key}
    try:
        status, payload, _ = request_json(url, "POST", 300, headers, body)
    except (RuntimeError, URLError, TimeoutError):
        return _read_status(config, request_json)
    if status == 200:
        return _snapshot(config, payload)
    if status == 409:
        return _read_status(config, request_json)
    if status in (401, 403, 404, 422, 503):
        raise RefreshRunClientError(f"refresh item advance returned HTTP {status}")
    return _read_status(config, request_json)


def _post_finalize(
    config: RefreshRunClientConfig,
    request_json: RequestJson,
) -> _Snapshot | None:
    url = f"{config.base_url}/api/refresh-runs/{config.run_id}/finalize"
    headers = {"Authorization": f"Bearer {config.token}"}
    try:
        status, payload, _ = request_json(url, "POST", 30, headers)
    except (RuntimeError, URLError, TimeoutError):
        return _read_status(config, request_json)
    if status == 200:
        return _snapshot(config, payload)
    if status in (401, 403, 404, 422, 503):
        raise RefreshRunClientError(f"refresh run finalization returned HTTP {status}")
    return _read_status(config, request_json)


def _next_item(snapshot: _Snapshot) -> _WorkItem | None:
    for item in snapshot.items:
        if item.status in ("pending", "claimed"):
            return item
        if item.status == "failed" and item.attempt < MAX_WORK_ITEM_ATTEMPTS:
            return item
    return None


def _active_claim(item: _WorkItem) -> bool:
    if item.status != "claimed" or item.claim_expires_at is None:
        return False
    expires_at = datetime.fromisoformat(item.claim_expires_at)
    return expires_at > datetime.now(timezone.utc)


def _final_result(snapshot: _Snapshot) -> _FinalResult:
    if snapshot.run.final_result is None:
        raise RefreshRunClientError("terminal refresh run has no final result")
    try:
        parsed: JsonValue = json.loads(snapshot.run.final_result)
        return _FinalResult.model_validate(parsed)
    except (json.JSONDecodeError, ValidationError) as exc:
        raise RefreshRunClientError("terminal refresh run has an invalid final result") from exc


def _client_result(snapshot: _Snapshot, started: float) -> RefreshRunClientResult:
    final = _final_result(snapshot)
    if final.status == "error":
        raise RefreshRunClientError("refresh run completed with no successful work items")
    return RefreshRunClientResult(
        status=final.status,
        tasks=[
            {"name": task.name, "status": task.status, "error_code": task.error_code}
            for task in final.tasks
        ],
        http_status=200,
        latency_seconds=round(time.perf_counter() - started, 3),
    )


def run_resumable_refresh(
    config: RefreshRunClientConfig,
    request_json: RequestJson,
) -> RefreshRunClientResult:
    started = time.perf_counter()
    snapshot = _start_run(config, request_json)
    deadline = started + config.max_wait_seconds
    for _attempt in range(1000):
        if snapshot.run.state in ("succeeded", "partial", "failed"):
            return _client_result(snapshot, started)
        item = _next_item(snapshot)
        if item is None:
            updated = _post_finalize(config, request_json)
            if updated is None:
                snapshot = _read_status(config, request_json)
                if snapshot is None:
                    raise RefreshRunClientError("refresh run disappeared during finalization")
            else:
                snapshot = updated
        elif _active_claim(item):
            if time.monotonic() >= deadline:
                raise RefreshRunClientError("refresh run exceeded the bounded wait time")
            time.sleep(2)
            current = _read_status(config, request_json)
            if current is None:
                raise RefreshRunClientError("refresh run disappeared while work was claimed")
            snapshot = current
        else:
            updated = _post_item(config, request_json, item.item_key)
            if updated is None:
                snapshot = _read_status(config, request_json)
                if snapshot is None:
                    raise RefreshRunClientError("refresh run disappeared after an uncertain advance")
            else:
                snapshot = updated
        if time.monotonic() >= deadline:
            raise RefreshRunClientError("refresh run exceeded the bounded wait time")
    raise RefreshRunClientError("refresh run exceeded the bounded operation count")
