from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone
from typing import Literal, TypeAlias, TypedDict

from pydantic import BaseModel, ConfigDict

from config import IPCA_GROUPS
from datafetchers.bcb_sgs import sgs_annual_windows
from db.refresh_run_models import (
    RefreshRunSnapshot,
    RefreshRunState,
    RefreshWorkItemStatus,
)
from db.refresh_runs import read_latest_run
from db.store import list_refresh_tasks

JsonScalar: TypeAlias = str | int | float | bool | None
JsonValue: TypeAlias = JsonScalar | list["JsonValue"] | dict[str, "JsonValue"]
TaskStatus: TypeAlias = Literal["ok", "partial", "error"]
_RETIRED_TASKS = frozenset({"polymarket"})


class RefreshTaskResult(TypedDict):
    name: str
    status: TaskStatus
    error_code: str | None


@dataclass(frozen=True, slots=True)
class RefreshTaskExecution:
    result: RefreshTaskResult
    series_updated: int
    record_count: int
    duration_ms: int


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


def _integer(value: str | float | bool | None) -> int:
    if isinstance(value, (str, int, float)):
        return int(value)
    return 0


def _latest_timestamp(values: list[str | None]) -> str | None:
    timestamps = [
        parsed.replace(tzinfo=timezone.utc) if parsed.tzinfo is None else parsed
        for value in values
        if value
        for parsed in (datetime.fromisoformat(value.replace("Z", "+00:00")),)
    ]
    return max(timestamps).isoformat() if timestamps else None


def _window_duration_ms(snapshot: RefreshRunSnapshot) -> int:
    duration = 0
    for item in snapshot.items:
        if not item.task.startswith("sgs_ipca_grupos_") or not item.claimed_at or not item.finished_at:
            continue
        claimed = datetime.fromisoformat(item.claimed_at.replace("Z", "+00:00"))
        finished = datetime.fromisoformat(item.finished_at.replace("Z", "+00:00"))
        duration += max(0, int((finished - claimed).total_seconds() * 1000))
    return duration


def _ipca_group_task_row(
    snapshot: RefreshRunSnapshot,
    existing: _RefreshTaskRow | None,
) -> dict[str, object]:
    expected = {
        f"sgs_ipca_grupos_{code}_{window}"
        for code in IPCA_GROUPS.values()
        for window in range(4)
    }
    windows = [item for item in snapshot.items if item.task.startswith("sgs_ipca_grupos_")]
    exact_manifest = len(windows) == len(expected) and {item.task for item in windows} == expected
    recognized = [item for item in windows if item.task in expected]
    statuses = [item.status for item in recognized]
    all_succeeded = exact_manifest and all(
        item.status is RefreshWorkItemStatus.SUCCEEDED for item in recognized
    )
    completed_data = [
        item for item in recognized
        if item.status in (RefreshWorkItemStatus.SUCCEEDED, RefreshWorkItemStatus.PARTIAL)
    ]
    has_unfinished = any(
        status in (RefreshWorkItemStatus.PENDING, RefreshWorkItemStatus.CLAIMED)
        for status in statuses
    ) or not exact_manifest
    if all_succeeded:
        status = "ok"
    elif has_unfinished or completed_data or any(
        value is RefreshWorkItemStatus.PARTIAL for value in statuses
    ):
        status = "partial"
    else:
        status = "error"

    successful_series = sum(
        all(
            any(
                item.task == f"sgs_ipca_grupos_{code}_{window}"
                and item.status is RefreshWorkItemStatus.SUCCEEDED
                for item in recognized
            )
            for window in range(4)
        )
        for code in IPCA_GROUPS.values()
    )
    old_last_success = existing.last_success if existing else None
    current_last_success = _latest_timestamp([
        item.finished_at
        for item in completed_data
        if item.series_updated > 0
    ])
    error_code = next(
        (item.error_code for item in recognized if item.error_code is not None),
        None,
    )
    if not exact_manifest:
        error_code = "IncompleteIPCAGroupManifest"

    return {
        "task": "sgs_ipca_grupos",
        "provider": existing.provider if existing else "bcb_sgs",
        "last_attempt": _latest_timestamp(
            [snapshot.run.created_at, *(item.claimed_at for item in recognized)]
        ),
        "last_success": _latest_timestamp([old_last_success, current_last_success]),
        "status": status,
        "series_expected": len(IPCA_GROUPS),
        "series_updated": successful_series,
        "record_count": sum(item.record_count for item in completed_data),
        "duration_ms": _window_duration_ms(snapshot),
        "error": error_code,
    }


def _current_ipca_groups(
    rows: list[_RefreshTaskRow],
    snapshot: RefreshRunSnapshot | None,
) -> list[_RefreshTaskRow]:
    if snapshot is None or snapshot.run.manifest_version not in {"daily-refresh-v2", "daily-refresh-v3", "daily-refresh-v4"}:
        return rows

    existing = next((row for row in rows if row.task == "sgs_ipca_grupos"), None)
    aggregate = _ipca_group_task_row(snapshot, existing)
    replacement = _RefreshTaskRow.model_validate(aggregate)
    return [
        replacement if row.task == "sgs_ipca_grupos" else row
        for row in rows
    ] if existing else [*rows, replacement]


def _interest_work_items(snapshot: RefreshRunSnapshot) -> tuple[set[str], list[object]]:
    cutoff = datetime.fromisoformat(snapshot.run.created_at.replace("Z", "+00:00")).date()
    expected = {"sgs_juros_other"}
    for code in (432, 226):
        for start_text, end_text in sgs_annual_windows("2015-01-01", cutoff.isoformat()):
            start_day, start_month, start_year = (int(part) for part in start_text.split("/"))
            end_day, end_month, end_year = (int(part) for part in end_text.split("/"))
            start = date(start_year, start_month, start_day).isoformat()
            end = date(end_year, end_month, end_day).isoformat()
            expected.add(f"sgs_juros_w{code}_{start}_{end}")
    items = [item for item in snapshot.items if item.task.startswith("sgs_juros_w") or item.task == "sgs_juros_other"]
    return expected, items


def _interest_task_row(
    snapshot: RefreshRunSnapshot,
    existing: _RefreshTaskRow | None,
) -> dict[str, object]:
    expected, items = _interest_work_items(snapshot)
    exact = {item.task for item in items} == expected
    recognized = [item for item in items if item.task in expected]
    all_success = exact and all(item.status is RefreshWorkItemStatus.SUCCEEDED for item in recognized)
    completed = [item for item in recognized if item.status in (RefreshWorkItemStatus.SUCCEEDED, RefreshWorkItemStatus.PARTIAL)]
    unfinished = not exact or any(item.status in (RefreshWorkItemStatus.PENDING, RefreshWorkItemStatus.CLAIMED) for item in recognized)
    if all_success:
        status = "ok"
    elif unfinished or completed or any(item.status is RefreshWorkItemStatus.PARTIAL for item in recognized):
        status = "partial"
    else:
        status = "error"
    success_by_code = {
        code: all(
            any(item.task == name and item.status is RefreshWorkItemStatus.SUCCEEDED for item in recognized)
            for name in expected if name.startswith(f"sgs_juros_w{code}_")
        ) and any(name.startswith(f"sgs_juros_w{code}_") for name in expected)
        for code in (432, 226)
    }
    successful_series = 2 if any(item.task == "sgs_juros_other" and item.status is RefreshWorkItemStatus.SUCCEEDED for item in recognized) else 0
    successful_series += sum(success_by_code.values())
    last_success = _latest_timestamp([
        existing.last_success if existing else None,
        *[item.finished_at for item in completed if item.series_updated > 0],
    ])
    return {
        "task": "sgs_juros",
        "provider": existing.provider if existing else "bcb_sgs",
        "last_attempt": _latest_timestamp([snapshot.run.created_at, *(item.claimed_at for item in recognized)]),
        "last_success": last_success,
        "status": status,
        "series_expected": 4,
        "series_updated": successful_series,
        "record_count": sum(item.record_count for item in completed),
        "duration_ms": sum(
            max(0, int((datetime.fromisoformat(item.finished_at.replace("Z", "+00:00")) - datetime.fromisoformat(item.claimed_at.replace("Z", "+00:00"))).total_seconds() * 1000))
            for item in completed if item.claimed_at and item.finished_at
        ),
        "error": "IncompleteSGSInterestManifest" if not exact else next((item.error_code for item in recognized if item.error_code), None),
    }


def _current_interest_group(
    rows: list[_RefreshTaskRow],
    snapshot: RefreshRunSnapshot | None,
) -> list[_RefreshTaskRow]:
    if snapshot is None or snapshot.run.manifest_version != "daily-refresh-v4":
        return rows
    existing = next((row for row in rows if row.task == "sgs_juros"), None)
    aggregate = _RefreshTaskRow.model_validate(_interest_task_row(snapshot, existing))
    return [aggregate if row.task == "sgs_juros" else row for row in rows] if existing else [*rows, aggregate]


def refresh_status_payload(now: datetime | None = None) -> dict[str, JsonValue]:
    generated = now or datetime.now(timezone.utc)
    snapshot = read_latest_run()
    rows = _current_interest_group(
        _current_ipca_groups(
            [
                _RefreshTaskRow.model_validate(row)
                for row in list_refresh_tasks()
                if row.get("task") not in _RETIRED_TASKS
            ],
            snapshot,
        ),
        snapshot,
    )
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
    if (
        snapshot is not None
        and snapshot.run.manifest_version in {"daily-refresh-v2", "daily-refresh-v3", "daily-refresh-v4"}
        and snapshot.run.state is not RefreshRunState.SUCCEEDED
        and overall == "ok"
    ):
        overall = "partial"
    return {"generated_at": generated.isoformat(), "overall_status": overall, "tasks": tasks}
