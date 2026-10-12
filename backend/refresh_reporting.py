from datetime import datetime, timedelta, timezone
from typing import Literal, TypeAlias, TypedDict

from pydantic import BaseModel, ConfigDict

from db.store import list_refresh_tasks

JsonScalar: TypeAlias = str | int | float | bool | None
JsonValue: TypeAlias = JsonScalar | list["JsonValue"] | dict[str, "JsonValue"]
TaskStatus: TypeAlias = Literal["ok", "partial", "error"]


class RefreshTaskResult(TypedDict):
    name: str
    status: TaskStatus
    error_code: str | None


class RefreshResult(TypedDict):
    status: TaskStatus
    tasks: list[RefreshTaskResult]


class _RefreshTaskRow(BaseModel):
    model_config = ConfigDict(frozen=True, extra="ignore")

    task: str
    provider: str
    last_attempt: str | None
    last_success: str | None
    status: str
    series_expected: str | int | float | bool | None
    series_updated: str | int | float | bool | None
    record_count: str | int | float | bool | None
    duration_ms: str | int | float | bool | None
    error: str | None


def _integer(value: str | int | float | bool | None) -> int:
    if isinstance(value, (str, int, float)):
        return int(value)
    return 0


def refresh_status_payload(now: datetime | None = None) -> dict[str, JsonValue]:
    generated = now or datetime.now(timezone.utc)
    rows = [_RefreshTaskRow.model_validate(row) for row in list_refresh_tasks()]
    tasks: list[JsonValue] = []
    usable = 0
    healthy = 0
    for row in rows:
        raw_success = row.last_success
        if raw_success:
            success_at = datetime.fromisoformat(raw_success.replace("Z", "+00:00"))
            if success_at.tzinfo is None:
                success_at = success_at.replace(tzinfo=timezone.utc)
            deadline = success_at + timedelta(hours=30)
            freshness = "stale" if generated > deadline else "ok"
            usable += 1
            if row.status == "ok" and freshness == "ok":
                healthy += 1
            sla_deadline: JsonValue = deadline.isoformat()
        else:
            freshness = "never"
            sla_deadline = None
        tasks.append({
            "task": row.task,
            "provider": row.provider,
            "last_attempt": row.last_attempt,
            "last_success": raw_success,
            "status": row.status,
            "series_expected": _integer(row.series_expected),
            "series_updated": _integer(row.series_updated),
            "record_count": _integer(row.record_count),
            "duration_ms": _integer(row.duration_ms),
            "freshness": freshness,
            "sla_deadline": sla_deadline,
            "error_code": row.error,
        })
    if not rows:
        overall = "never_run"
    elif healthy == len(rows):
        overall = "ok"
    elif usable:
        overall = "partial"
    else:
        overall = "error"
    return {"generated_at": generated.isoformat(), "overall_status": overall, "tasks": tasks}
