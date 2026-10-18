from datetime import datetime, timezone
from enum import StrEnum
from time import perf_counter
from typing import assert_never

from db.refresh_run_models import (
    RefreshPersistenceError,
    RefreshRunCutoffPhase,
    RefreshRunCutoffUpdate,
    RefreshRunSnapshot,
    RefreshWorkItemStatus,
)
from db.refresh_runs import persist_run_cutoff
from db.store import record_refresh_task_finish, record_refresh_task_start
from derived.refresh import DerivedRefreshError
from refresh import (
    RefreshDependencies,
    _tasks,
    run_refresh_task_detailed,
)
from refresh_reporting import RefreshTaskExecution, TaskStatus
from refresh_run_state import lease_expiry


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
    source_tasks = {task.name: task for task in _tasks(snapshot.run.force, dependencies)}
    source_task = source_tasks.get(item_task)
    if source_task is not None:
        return run_refresh_task_detailed(source_task)

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
