from collections.abc import Mapping
from dataclasses import replace
from datetime import date, datetime, timezone
from enum import StrEnum
from time import monotonic, perf_counter
from typing import assert_never

from config import INFLATION, IPCA_GROUPS
from datafetchers.bcb_sgs import sgs_annual_windows, sgs_refresh_windows
from db.refresh_run_models import (
    RefreshPersistenceError,
    RefreshRunCutoffPhase,
    RefreshRunCutoffUpdate,
    RefreshRunSnapshot,
    RefreshWorkItemStatus,
)
from db.refresh_runs import persist_run_cutoff
from db.store import record_refresh_task_finish, record_refresh_task_start, set_meta
from derived.refresh import DerivedRefreshError
from refresh import (
    RefreshDependencies,
    _tasks,
    ipca_group_window_tasks,
    run_refresh_task_detailed,
    sgs_inflation_window_tasks,
    sgs_interest_remainder_task,
    sgs_interest_window_tasks,
)
from refresh_reporting import RefreshTaskExecution, TaskStatus
from refresh_run_state import lease_expiry

_LEGACY_IPCA_BUDGET_SECONDS = 220
_LEGACY_IPCA_REQUEST_TIMEOUT_SECONDS = 5


class RefreshPhaseTask(StrEnum):
    INFLATION_TARGET_RULES = "inflation_target_rules"
    DERIVED_SNAPSHOTS = "derived_snapshots"
    MACRO_SCORE = "macro_score"


class RefreshTaskDependencyError(RuntimeError):
    def __init__(self, task_name: str) -> None:
        self.task_name = task_name
        super().__init__(str(self))

    def __str__(self) -> str:
        return f"refresh callback is not configured for {self.task_name}"


def _mark_ipca_series_fresh(
    task_name: str,
    snapshot: RefreshRunSnapshot,
    payload: dict[str, object],
) -> None:
    parts = task_name.split("_")
    if len(parts) != 5 or parts[:3] != ["sgs", "ipca", "grupos"]:
        return
    try:
        code = int(parts[3])
    except ValueError:
        return
    series_name = next(
        (name for name, series_code in IPCA_GROUPS.items() if series_code == code),
        None,
    )
    if series_name is None:
        return
    siblings = tuple(
        item
        for item in snapshot.items
        if item.task.startswith(f"sgs_ipca_grupos_{code}_") and item.task != task_name
    )
    if any(item.status is not RefreshWorkItemStatus.SUCCEEDED for item in siblings):
        return
    records = payload.get(series_name)
    if not isinstance(records, list) or not records:
        return
    updated_at = datetime.now(timezone.utc).isoformat()
    set_meta(f"sgs_last_refresh_{code}", updated_at)
    set_meta("last_successful_update", updated_at)


def _interest_window_code(task_name: str) -> int | None:
    parts = task_name.split("_")
    if len(parts) != 5 or parts[:2] != ["sgs", "juros"] or not parts[2].startswith("w"):
        return None
    try:
        return int(parts[2][1:])
    except ValueError:
        return None


def _inflation_window_code(task_name: str) -> int | None:
    parts = task_name.split("_")
    if len(parts) != 5 or parts[:2] != ["sgs", "inflacao"] or not parts[2].startswith("w"):
        return None
    try:
        code = int(parts[2][1:])
    except ValueError:
        return None
    return code if code in INFLATION.values() else None


def _mark_inflation_window_fresh(
    task_name: str,
    snapshot: RefreshRunSnapshot,
    payload: Mapping[str, object],
) -> None:
    code = _inflation_window_code(task_name)
    if code is None or snapshot.run.manifest_version != "daily-refresh-v5":
        return
    series_name = next((name for name, value in INFLATION.items() if value == code), None)
    rows = payload.get(series_name) if series_name is not None else None
    if not isinstance(rows, list) or not rows:
        return
    cutoff = datetime.fromisoformat(snapshot.run.created_at.replace("Z", "+00:00")).date()
    expected = {
        f"sgs_inflacao_w{series_code}_{_window_date(start_text).isoformat()}_"
        f"{_window_date(end_text).isoformat()}"
        for series_code in INFLATION.values()
        for start_text, end_text in sgs_annual_windows("2015-01-01", cutoff.isoformat())
    }
    current_windows = [
        item for item in snapshot.items if item.task.startswith("sgs_inflacao_w")
    ]
    if {item.task for item in current_windows} != expected:
        return
    series_windows = [item for item in current_windows if item.task.startswith(f"sgs_inflacao_w{code}_")]
    if any(
        item.task != task_name and item.status is not RefreshWorkItemStatus.SUCCEEDED
        for item in series_windows
    ):
        return
    updated_at = datetime.now(timezone.utc).isoformat()
    set_meta(f"sgs_last_refresh_{code}", updated_at)
    if all(
        item.status is RefreshWorkItemStatus.SUCCEEDED
        for item in current_windows
        if item.task != task_name
    ):
        set_meta("last_successful_update", updated_at)


def _mark_interest_window_fresh(
    task_name: str,
    snapshot: RefreshRunSnapshot,
    payload: dict[str, object],
) -> None:
    code = _interest_window_code(task_name)
    if code not in {432, 226}:
        return
    series_name = "selic_meta" if code == 432 else "tr"
    records = payload.get(series_name)
    if not isinstance(records, list) or not records:
        return
    cutoff = datetime.fromisoformat(snapshot.run.created_at.replace("Z", "+00:00")).date()
    expected = {
        f"sgs_juros_w{code}_{_window_date(start_text).isoformat()}_"
        f"{_window_date(end_text).isoformat()}"
        for start_text, end_text in sgs_annual_windows("2015-01-01", cutoff.isoformat())
    }
    siblings = [item for item in snapshot.items if item.task.startswith(f"sgs_juros_w{code}_")]
    if {item.task for item in siblings} != expected:
        return
    if any(
        item.task != task_name and item.status is not RefreshWorkItemStatus.SUCCEEDED
        for item in siblings
    ):
        return
    updated_at = datetime.now(timezone.utc).isoformat()
    set_meta(f"sgs_last_refresh_{code}", updated_at)
    other_expected = next((item for item in snapshot.items if item.task == "sgs_juros_other"), None)
    other_windows = [item for item in snapshot.items if item.task.startswith("sgs_juros_w")]
    all_codes_done = all(
        item.task == task_name or item.status is RefreshWorkItemStatus.SUCCEEDED
        for item in other_windows
    ) and other_expected is not None and (
        other_expected.status is RefreshWorkItemStatus.SUCCEEDED
    )
    if all_codes_done:
        set_meta("last_successful_update", updated_at)


def _window_date(value: str) -> date:
    day, month, year = (int(part) for part in value.split("/"))
    return date(year, month, day)


def _mark_interest_remainder_fresh(
    snapshot: RefreshRunSnapshot,
    payload: dict[str, object],
) -> None:
    required = {"selic_efetiva", "cdi"}
    if any(not isinstance(payload.get(name), list) or not payload[name] for name in required):
        return
    updated_at = datetime.now(timezone.utc).isoformat()
    for code in (11, 12):
        set_meta(f"sgs_last_refresh_{code}", updated_at)
    windows = [item for item in snapshot.items if item.task.startswith("sgs_juros_w")]
    if all(item.status is RefreshWorkItemStatus.SUCCEEDED for item in windows):
        set_meta("last_successful_update", updated_at)


def _run_legacy_ipca_group(dependencies: RefreshDependencies, force: bool) -> dict[str, object]:
    """Bound retries of an old monolithic manifest item while keeping its key stable."""
    deadline = monotonic() + _LEGACY_IPCA_BUDGET_SECONDS
    result: dict[str, object] = {}
    windows = sgs_refresh_windows()
    for series_name, code in IPCA_GROUPS.items():
        observations: list[object] = []
        for start, end in windows:
            remaining = deadline - monotonic()
            if remaining <= 0:
                raise TimeoutError("legacy SGS IPCA group item exceeded its 220-second budget")
            payload = dependencies.fetch_sgs_batch(
                {series_name: code},
                start_date=start,
                end_date=end,
                use_cache=not force,
                mark_refreshed=False,
                fail_fast=True,
                request_timeout=min(_LEGACY_IPCA_REQUEST_TIMEOUT_SECONDS, remaining),
            )
            rows = payload.get(series_name)
            if not isinstance(rows, list):
                raise TypeError(f"SGS IPCA series {code} window returned an invalid payload")
            observations.extend(rows)
        if not observations:
            raise RuntimeError(f"SGS IPCA series {code} returned no observations")
        result[series_name] = observations
    updated_at = datetime.now(timezone.utc).isoformat()
    for code in IPCA_GROUPS.values():
        set_meta(f"sgs_last_refresh_{code}", updated_at)
    set_meta("last_successful_update", updated_at)
    return result


def _task_result(
    name: str,
    status: TaskStatus,
    error_code: str | None,
    series_updated: int,
    record_count: int,
    duration_ms: int,
) -> RefreshTaskExecution:
    return RefreshTaskExecution(
        {"name": name, "status": status, "error_code": error_code},
        series_updated,
        record_count,
        duration_ms,
    )


def _execute_phase_task(
    phase: RefreshPhaseTask,
    dependencies: RefreshDependencies,
    as_of: datetime,
    cycle_id: str,
) -> RefreshTaskExecution:
    name = phase.value
    provider = "macro_score" if phase is RefreshPhaseTask.MACRO_SCORE else "derived"
    series_expected = 11 if phase is RefreshPhaseTask.INFLATION_TARGET_RULES else 1
    started_at = datetime.now(timezone.utc)
    record_refresh_task_start(name, provider, series_expected, started_at.isoformat())
    timer = perf_counter()
    series_updated = 0
    record_count = 0
    status: TaskStatus = "ok"
    error_code: str | None = None
    try:
        match phase:
            case RefreshPhaseTask.INFLATION_TARGET_RULES:
                callback = dependencies.observe_inflation_target_rules
                if callback is None:
                    raise RefreshTaskDependencyError(name)
                series_updated = callback()
                record_count = series_updated
            case RefreshPhaseTask.DERIVED_SNAPSHOTS:
                series_updated = dependencies.refresh_derived_snapshots(as_of, cycle_id)
                record_count = series_updated
            case RefreshPhaseTask.MACRO_SCORE:
                callback = dependencies.refresh_macro_score
                if callback is None:
                    raise RefreshTaskDependencyError(name)
                series_updated = callback(as_of, cycle_id)
                record_count = series_updated
            case unreachable:
                assert_never(unreachable)
    except DerivedRefreshError as exc:
        series_updated = exc.inserted_count
        record_count = exc.inserted_count
        status = "error"
        error_code = type(exc).__name__[:80]
    except Exception as exc:  # noqa: BLE001 - phase boundary preserves task isolation.
        series_updated = 0
        record_count = 0
        status = "error"
        error_code = type(exc).__name__[:80]
    duration_ms = round((perf_counter() - timer) * 1000)
    record_refresh_task_finish(
        name,
        status,
        series_updated,
        record_count,
        duration_ms,
        error_code,
        datetime.now(timezone.utc).isoformat(),
    )
    return _task_result(name, status, error_code, series_updated, record_count, duration_ms)


def _record_skipped_phase_task(phase: RefreshPhaseTask, now: datetime) -> RefreshTaskExecution:
    name = phase.value
    provider = "macro_score" if phase is RefreshPhaseTask.MACRO_SCORE else "derived"
    series_expected = 11 if phase is RefreshPhaseTask.INFLATION_TARGET_RULES else 1
    error_code = "NoSuccessfulSourceTasks"
    record_refresh_task_start(name, provider, series_expected, now.isoformat())
    record_refresh_task_finish(name, "error", 0, 0, 0, error_code, now.isoformat())
    return _task_result(name, "error", error_code, 0, 0, 0)


def execute_refresh_item(
    item_task: str,
    snapshot: RefreshRunSnapshot,
    dependencies: RefreshDependencies,
    now: datetime,
) -> RefreshTaskExecution:
    source_task_list = [
        *_tasks(snapshot.run.force, dependencies),
        *ipca_group_window_tasks(snapshot.run.force, dependencies),
    ]
    if snapshot.run.manifest_version == "daily-refresh-v5":
        cutoff = datetime.fromisoformat(snapshot.run.created_at.replace("Z", "+00:00")).date()
        source_task_list.extend(sgs_inflation_window_tasks(snapshot.run.force, dependencies, cutoff))
    if snapshot.run.manifest_version in {"daily-refresh-v4", "daily-refresh-v5"}:
        cutoff = datetime.fromisoformat(snapshot.run.created_at.replace("Z", "+00:00")).date()
        source_task_list.extend(sgs_interest_window_tasks(snapshot.run.force, dependencies, cutoff))
        source_task_list.append(sgs_interest_remainder_task(snapshot.run.force, dependencies))
    source_tasks = {task.name: task for task in source_task_list}
    source_task = source_tasks.get(item_task)
    if source_task is None and item_task == "polymarket" and snapshot.run.manifest_version in {
        "daily-refresh-v1", "daily-refresh-v2",
    }:
        # Complete the immutable old manifest without invoking the retired source.
        return RefreshTaskExecution(
            {"name": "polymarket", "status": "error", "error_code": "retired_source"},
            0, 0, 0,
        )
    if source_task is not None:
        if item_task == "sgs_ipca_grupos" and snapshot.run.manifest_version == "daily-refresh-v1":
            bounded_task = replace(
                source_task,
                run=lambda: _run_legacy_ipca_group(dependencies, snapshot.run.force),
            )
            return run_refresh_task_detailed(
                bounded_task, run_id=str(snapshot.run.run_id), cycle_id=str(snapshot.run.cycle_id)
            )
        if item_task.startswith("sgs_ipca_grupos_") and item_task != "sgs_ipca_grupos":
            def run_window() -> dict[str, object]:
                payload = source_task.run()
                _mark_ipca_series_fresh(item_task, snapshot, payload)
                return payload

            return run_refresh_task_detailed(
                replace(source_task, run=run_window),
                run_id=str(snapshot.run.run_id), cycle_id=str(snapshot.run.cycle_id),
            )
        if _inflation_window_code(item_task) is not None:
            def run_inflation_window() -> dict[str, object]:
                payload = dict(source_task.run())
                _mark_inflation_window_fresh(item_task, snapshot, payload)
                return payload

            return run_refresh_task_detailed(
                replace(source_task, run=run_inflation_window),
                run_id=str(snapshot.run.run_id), cycle_id=str(snapshot.run.cycle_id),
            )
        if _interest_window_code(item_task) is not None:
            def run_interest_window() -> dict[str, object]:
                payload = source_task.run()
                _mark_interest_window_fresh(item_task, snapshot, payload)
                return payload

            return run_refresh_task_detailed(
                replace(source_task, run=run_interest_window),
                run_id=str(snapshot.run.run_id), cycle_id=str(snapshot.run.cycle_id),
            )
        if item_task == "sgs_juros_other":
            def run_interest_remainder() -> dict[str, object]:
                payload = source_task.run()
                _mark_interest_remainder_fresh(snapshot, payload)
                return payload

            return run_refresh_task_detailed(
                replace(source_task, run=run_interest_remainder),
                run_id=str(snapshot.run.run_id), cycle_id=str(snapshot.run.cycle_id),
            )
        return run_refresh_task_detailed(
            source_task, run_id=str(snapshot.run.run_id), cycle_id=str(snapshot.run.cycle_id)
        )

    phase = RefreshPhaseTask(item_task)
    phase_names = frozenset(task.value for task in RefreshPhaseTask)
    source_items = tuple(item for item in snapshot.items if item.task not in phase_names)
    if source_items and all(item.status is RefreshWorkItemStatus.FAILED for item in source_items):
        return _record_skipped_phase_task(phase, now)
    match phase:
        case RefreshPhaseTask.INFLATION_TARGET_RULES:
            cutoff = now
        case RefreshPhaseTask.DERIVED_SNAPSHOTS:
            persisted_cutoff = persist_run_cutoff(
                RefreshRunCutoffUpdate(
                    snapshot.run.run_id,
                    RefreshRunCutoffPhase.DERIVED,
                    now.isoformat(),
                    now.isoformat(),
                    lease_expiry(now),
                )
            )
            cutoff = datetime.fromisoformat(persisted_cutoff)
        case RefreshPhaseTask.MACRO_SCORE:
            if snapshot.run.derived_cutoff is None:
                raise RefreshPersistenceError(snapshot.run.run_id)
            persisted_cutoff = persist_run_cutoff(
                RefreshRunCutoffUpdate(
                    snapshot.run.run_id,
                    RefreshRunCutoffPhase.MACRO_SCORE,
                    snapshot.run.derived_cutoff,
                    now.isoformat(),
                    lease_expiry(now),
                )
            )
            cutoff = datetime.fromisoformat(persisted_cutoff)
        case unreachable:
            assert_never(unreachable)
    return _execute_phase_task(phase, dependencies, cutoff, str(snapshot.run.cycle_id))
