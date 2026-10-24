import json
from dataclasses import dataclass, replace
from datetime import datetime, timedelta, timezone
from typing import Final, Literal, TypeAlias, assert_never

from db.refresh_run_models import (
    MAX_WORK_ITEM_ATTEMPTS,
    CycleId,
    RefreshPersistenceError,
    RefreshRunFinalization,
    RefreshRunFinalState,
    RefreshRunSnapshot,
    RefreshRunStart,
    RefreshRunState,
    RefreshWorkItemClaimRequest,
    RefreshWorkItemCompletion,
    RefreshWorkItemOutcome,
    RefreshWorkItemRecord,
    RefreshWorkItemSpec,
    RefreshWorkItemStatus,
    RunId,
    WorkItemKey,
)
from db.refresh_runs import (
    claim_next_work_item,
    complete_work_item_claim,
    create_or_resume_run,
    finalize_and_release_run,
    read_run,
)
from derived.inflation_target_inputs import observe_inflation_target_rules
from macro_score.refresh import refresh_macro_score
from refresh import RefreshDependencies, _tasks, ipca_group_window_tasks
from refresh_reporting import RefreshTaskExecution, TaskStatus
from refresh_run_state import (
    RefreshRunNotFoundError,
    get_refresh_run,
    lease_expiry,
    resume_refresh_run,
)
from refresh_run_tasks import RefreshPhaseTask, execute_refresh_item

_MANIFEST_VERSION: Final = "daily-refresh-v2"
_CLAIM_SECONDS: Final = 10 * 60
_FinalTaskStatus: TypeAlias = TaskStatus | Literal["skipped"]


@dataclass(frozen=True, slots=True)
class RefreshRunStartRequest:
    run_id: RunId
    cycle_id: CycleId
    force: bool


@dataclass(frozen=True, slots=True)
class RefreshRunAdvanceRequest:
    run_id: RunId
    expected_item_key: WorkItemKey


def _dependencies(value: RefreshDependencies | None) -> RefreshDependencies:
    if value is not None:
        return value
    dependencies = RefreshDependencies()
    return replace(
        dependencies,
        observe_inflation_target_rules=(
            dependencies.observe_inflation_target_rules or observe_inflation_target_rules
        ),
        refresh_macro_score=dependencies.refresh_macro_score or refresh_macro_score,
    )


def _manifest(
    force: bool,
    dependencies: RefreshDependencies,
    version: str = _MANIFEST_VERSION,
) -> tuple[RefreshWorkItemSpec, ...]:
    source_tasks = _tasks(force, dependencies)
    names: list[str] = []
    for task in source_tasks:
        if version == _MANIFEST_VERSION and task.name == "sgs_ipca_grupos":
            names.extend(item.name for item in ipca_group_window_tasks(force, dependencies))
        else:
            names.append(task.name)
    if dependencies.observe_inflation_target_rules is not None:
        names.append(RefreshPhaseTask.INFLATION_TARGET_RULES.value)
    names.append(RefreshPhaseTask.DERIVED_SNAPSHOTS.value)
    if dependencies.refresh_macro_score is not None:
        names.append(RefreshPhaseTask.MACRO_SCORE.value)
    return tuple(RefreshWorkItemSpec(WorkItemKey(name), name, order) for order, name in enumerate(names))


def start_refresh_run(
    request: RefreshRunStartRequest,
    dependencies: RefreshDependencies | None = None,
) -> RefreshRunSnapshot:
    now = datetime.now(timezone.utc)
    active_dependencies = _dependencies(dependencies)
    existing = read_run(request.run_id)
    if existing is None:
        manifest_version = _MANIFEST_VERSION
        items = _manifest(request.force, active_dependencies, manifest_version)
    else:
        # A retried workflow must resume the exact persisted manifest. This
        # keeps old run IDs idempotent while newer runs use smaller work items.
        manifest_version = existing.run.manifest_version
        items = tuple(
            RefreshWorkItemSpec(item.item_key, item.task, item.task_order)
            for item in existing.items
        )
    return create_or_resume_run(
        RefreshRunStart(
            run_id=request.run_id,
            cycle_id=request.cycle_id,
            force=request.force,
            manifest_version=manifest_version,
            derived_cutoff=None,
            macro_cutoff=None,
            items=items,
            now=now.isoformat(),
            lease_expires_at=lease_expiry(now),
        )
    )


def _completion(
    snapshot: RefreshRunSnapshot,
    claim: RefreshWorkItemRecord,
    execution: RefreshTaskExecution,
    now: datetime,
) -> RefreshWorkItemCompletion:
    if execution.result["error_code"] == "NoSuccessfulSourceTasks":
        outcome = RefreshWorkItemOutcome.SKIPPED
    else:
        match execution.result["status"]:
            case "ok":
                outcome = RefreshWorkItemOutcome.SUCCEEDED
            case "partial":
                outcome = RefreshWorkItemOutcome.PARTIAL
            case "error":
                outcome = RefreshWorkItemOutcome.FAILED
            case unreachable:
                assert_never(unreachable)
    if claim.claim_token is None:
        raise RefreshPersistenceError(snapshot.run.run_id)
    return RefreshWorkItemCompletion(
        snapshot.run.run_id,
        claim.item_key,
        claim.claim_token,
        outcome,
        now.isoformat(),
        execution.series_updated,
        execution.record_count,
        execution.result["error_code"],
    )


def _final_state(snapshot: RefreshRunSnapshot) -> RefreshRunFinalState:
    statuses = tuple(item.status for item in snapshot.items)
    if all(status is RefreshWorkItemStatus.SUCCEEDED for status in statuses):
        return RefreshRunFinalState.SUCCEEDED
    if all(
        status in (RefreshWorkItemStatus.FAILED, RefreshWorkItemStatus.SKIPPED)
        for status in statuses
    ):
        return RefreshRunFinalState.FAILED
    return RefreshRunFinalState.PARTIAL


def _final_result(snapshot: RefreshRunSnapshot) -> str:
    results: list[dict[str, str | None]] = []
    for item in snapshot.items:
        match item.status:
            case RefreshWorkItemStatus.SUCCEEDED:
                status: _FinalTaskStatus = "ok"
            case RefreshWorkItemStatus.PARTIAL:
                status = "partial"
            case RefreshWorkItemStatus.FAILED:
                status = "error"
            case RefreshWorkItemStatus.SKIPPED:
                status = "skipped"
            case RefreshWorkItemStatus.PENDING | RefreshWorkItemStatus.CLAIMED:
                continue
            case unreachable:
                assert_never(unreachable)
        results.append({"name": item.task, "status": status, "error_code": item.error_code})
    if all(result["status"] == "ok" for result in results):
        status: TaskStatus = "ok"
    elif any(result["status"] in ("ok", "partial") for result in results):
        status = "partial"
    else:
        status = "error"
    return json.dumps({"status": status, "tasks": results}, sort_keys=True, separators=(",", ":"))


def _finalize_if_resolved(snapshot: RefreshRunSnapshot, now: datetime) -> RefreshRunSnapshot:
    if snapshot.run.state is not RefreshRunState.RUNNING:
        return snapshot
    snapshot = resume_refresh_run(snapshot, now)
    if snapshot.run.state is not RefreshRunState.RUNNING:
        return snapshot
    finalized = finalize_and_release_run(
        RefreshRunFinalization(
            snapshot.run.run_id,
            _final_state(snapshot),
            _final_result(snapshot),
            now.isoformat(),
            now.isoformat()
            if any(
                item.status in (RefreshWorkItemStatus.SUCCEEDED, RefreshWorkItemStatus.PARTIAL)
                for item in snapshot.items
            )
            else None,
        )
    )
    if not finalized:
        return get_refresh_run(snapshot.run.run_id)
    return get_refresh_run(snapshot.run.run_id)


def advance_refresh_run(
    request: RefreshRunAdvanceRequest,
    dependencies: RefreshDependencies | None = None,
) -> RefreshRunSnapshot:
    active_dependencies = _dependencies(dependencies)
    now = datetime.now(timezone.utc)
    snapshot = resume_refresh_run(get_refresh_run(request.run_id), now)
    if snapshot.run.state is not RefreshRunState.RUNNING:
        return snapshot
    expected = next(
        (item for item in snapshot.items if item.item_key == request.expected_item_key),
        None,
    )
    if expected is None:
        raise RefreshRunNotFoundError(request.run_id)
    match expected.status:
        case RefreshWorkItemStatus.SUCCEEDED | RefreshWorkItemStatus.PARTIAL:
            resolved = True
        case RefreshWorkItemStatus.FAILED:
            resolved = expected.attempt >= MAX_WORK_ITEM_ATTEMPTS
        case RefreshWorkItemStatus.PENDING | RefreshWorkItemStatus.CLAIMED:
            resolved = False
        case unreachable:
            assert_never(unreachable)
    if resolved:
        return _finalize_if_resolved(snapshot, now)
    claim_expiry = now + timedelta(seconds=_CLAIM_SECONDS)
    claim = claim_next_work_item(
        RefreshWorkItemClaimRequest(
            request.run_id,
            now.isoformat(),
            claim_expiry.isoformat(),
            request.expected_item_key,
        )
    )
    if claim is None:
        return get_refresh_run(request.run_id)
    execution = execute_refresh_item(claim.task, snapshot, active_dependencies, now)
    if complete_work_item_claim(_completion(snapshot, claim, execution, datetime.now(timezone.utc))):
        updated = get_refresh_run(request.run_id)
        return _finalize_if_resolved(updated, datetime.now(timezone.utc))
    return get_refresh_run(request.run_id)


def finalize_refresh_run(run_id: RunId) -> RefreshRunSnapshot:
    return _finalize_if_resolved(get_refresh_run(run_id), datetime.now(timezone.utc))
