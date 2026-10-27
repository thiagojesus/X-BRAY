from collections.abc import Mapping
from datetime import datetime
from typing import Annotated, assert_never

from fastapi import APIRouter, Depends, HTTPException, Path, status
from pydantic import BaseModel, ConfigDict, Field

from db.refresh_run_models import (
    MAX_WORK_ITEM_ATTEMPTS,
    CycleId,
    RefreshLeaseConflictError,
    RefreshRunIdempotencyError,
    RefreshRunRecord,
    RefreshRunSnapshot,
    RefreshRunState,
    RefreshWorkItemRecord,
    RefreshWorkItemStatus,
    RunId,
    WorkItemKey,
)
from refresh_auth import require_refresh_auth
from refresh_run_execution import (
    RefreshRunAdvanceRequest,
    RefreshRunNotFoundError,
    RefreshRunStartRequest,
    advance_refresh_run,
    finalize_refresh_run,
    get_refresh_run,
    start_refresh_run,
)

router = APIRouter(prefix="/api/refresh-runs", tags=["refresh-runs"])


class RefreshRunStartBody(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    run_id: str = Field(min_length=1, max_length=128)
    cycle_id: str = Field(min_length=1, max_length=128)
    force: bool = False


class RefreshRunAdvanceBody(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    expected_item_key: str = Field(min_length=1, max_length=128)


class RefreshRunResponse(BaseModel):
    model_config = ConfigDict(frozen=True)

    run_id: str
    cycle_id: str
    state: RefreshRunState
    force: bool
    manifest_version: str
    derived_cutoff: str | None
    macro_cutoff: str | None
    created_at: str
    updated_at: str
    finished_at: str | None
    final_result: str | None
    lease_expires_at: str | None
    next_due_at: str | None = None
    due_work_item_key: str | None = None


class RefreshWorkItemResponse(BaseModel):
    model_config = ConfigDict(frozen=True)

    item_key: str
    task: str
    task_order: int
    status: RefreshWorkItemStatus
    attempt: int
    claim_expires_at: str | None
    claimed_at: str | None
    created_at: str
    updated_at: str
    finished_at: str | None
    series_updated: int
    record_count: int
    error_code: str | None
    claim_generation: int = 0
    acquisition_attempts: int = 0
    next_attempt_at: str | None = None
    deferral_reason: str | None = None
    last_error_category: str | None = None
    last_error_code: str | None = None


class RefreshRunSnapshotResponse(BaseModel):
    model_config = ConfigDict(frozen=True)

    run: RefreshRunResponse
    items: tuple[RefreshWorkItemResponse, ...]


def _run_response(
    record: RefreshRunRecord,
    lease_expires_at: str | None,
    next_due_at: str | None = None,
    due_work_item_key: str | None = None,
) -> RefreshRunResponse:
    return RefreshRunResponse(
        run_id=record.run_id,
        cycle_id=record.cycle_id,
        state=record.state,
        force=record.force,
        manifest_version=record.manifest_version,
        derived_cutoff=record.derived_cutoff,
        macro_cutoff=record.macro_cutoff,
        created_at=record.created_at,
        updated_at=record.updated_at,
        finished_at=record.finished_at,
        final_result=record.final_result,
        lease_expires_at=lease_expires_at,
        next_due_at=next_due_at,
        due_work_item_key=due_work_item_key,
    )


def _item_response(item: RefreshWorkItemRecord) -> RefreshWorkItemResponse:
    return RefreshWorkItemResponse(
        item_key=item.item_key,
        task=item.task,
        task_order=item.task_order,
        status=item.status,
        attempt=item.attempt,
        claim_expires_at=item.claim_expires_at,
        claimed_at=item.claimed_at,
        created_at=item.created_at,
        updated_at=item.updated_at,
        finished_at=item.finished_at,
        series_updated=item.series_updated,
        record_count=item.record_count,
        error_code=item.error_code,
        claim_generation=item.claim_generation,
        acquisition_attempts=item.acquisition_attempts,
        next_attempt_at=item.next_attempt_at,
        deferral_reason=item.deferral_reason,
        last_error_category=item.last_error_category,
        last_error_code=item.last_error_code,
    )


def _snapshot_response(snapshot: RefreshRunSnapshot) -> RefreshRunSnapshotResponse:
    from datetime import timezone

    from db.store import source_access_states
    from refresh_run_scheduler import item_eligibility

    now = datetime.now(timezone.utc)
    states = source_access_states()
    due_key = _due_work_item_key(snapshot, states, now)
    due_values: list[tuple[datetime, str]] = []
    for item in snapshot.items:
        if item.status not in {RefreshWorkItemStatus.PENDING, RefreshWorkItemStatus.FAILED}:
            continue
        if item.acquisition_attempts >= MAX_WORK_ITEM_ATTEMPTS:
            continue
        item_due = None
        if item.next_attempt_at is not None:
            try:
                item_due = datetime.fromisoformat(item.next_attempt_at.replace("Z", "+00:00"))
            except ValueError:
                item_due = None
        _, provider_due, _ = item_eligibility(item.task, states, now)
        if provider_due is not None:
            try:
                parsed_due = datetime.fromisoformat(provider_due.replace("Z", "+00:00"))
                due_values.append((max(parsed_due, item_due) if item_due else parsed_due, str(item.item_key)))
            except ValueError:
                continue
        elif item_due is not None:
            due_values.append((item_due, str(item.item_key)))
    next_due_at = min(due_values, default=(None, None), key=lambda value: value[0])[0]
    return RefreshRunSnapshotResponse(
        run=_run_response(
            snapshot.run,
            snapshot.lease_expires_at,
            next_due_at.isoformat() if next_due_at else None,
            due_key,
        ),
        items=tuple(_item_response(item) for item in snapshot.items),
    )


def _due_work_item_key(
    snapshot: RefreshRunSnapshot,
    states: Mapping[str, Mapping[str, object]],
    now: datetime,
) -> str | None:
    from refresh_run_scheduler import item_eligibility

    ordered = sorted(snapshot.items, key=lambda item: item.task_order)
    if any(item.status is RefreshWorkItemStatus.CLAIMED for item in ordered):
        return None
    for index, item in enumerate(ordered):
        if item.status not in {RefreshWorkItemStatus.PENDING, RefreshWorkItemStatus.FAILED}:
            continue
        if item.acquisition_attempts >= MAX_WORK_ITEM_ATTEMPTS:
            continue
        if item.next_attempt_at is not None:
            try:
                due = datetime.fromisoformat(item.next_attempt_at.replace("Z", "+00:00"))
            except ValueError:
                continue
            if due > now:
                continue
        eligible, _, _ = item_eligibility(item.task, states, now)
        if not eligible:
            continue
        blocked = False
        for earlier in ordered[:index]:
            if earlier.status in {
                RefreshWorkItemStatus.SUCCEEDED,
                RefreshWorkItemStatus.PARTIAL,
                RefreshWorkItemStatus.SKIPPED,
            }:
                continue
            if earlier.status not in {
                RefreshWorkItemStatus.PENDING,
                RefreshWorkItemStatus.FAILED,
                RefreshWorkItemStatus.CLAIMED,
            }:
                continue
            if earlier.acquisition_attempts >= MAX_WORK_ITEM_ATTEMPTS:
                continue
            if earlier.next_attempt_at is not None:
                try:
                    earlier_due = datetime.fromisoformat(earlier.next_attempt_at.replace("Z", "+00:00"))
                except ValueError:
                    earlier_due = now
                if earlier_due > now and item.task not in {
                    "derived_snapshots", "macro_score", "inflation_target_rules",
                }:
                    continue
                if earlier_due > now:
                    blocked = True
                    break
            prior_eligible, _, _ = item_eligibility(earlier.task, states, now)
            if prior_eligible:
                blocked = True
                break
        if not blocked:
            return str(item.item_key)
    return None


def _raise_conflict(error: RefreshLeaseConflictError | RefreshRunIdempotencyError) -> None:
    match error:
        case RefreshLeaseConflictError():
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT,
                detail={"error": "refresh_lease_conflict"},
                headers={"Retry-After": "60"},
            ) from error
        case RefreshRunIdempotencyError():
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT,
                detail={"error": "refresh_run_manifest_conflict"},
            ) from error
        case unreachable:
            assert_never(unreachable)


def _raise_not_found(error: RefreshRunNotFoundError) -> None:
    raise HTTPException(
        status_code=status.HTTP_404_NOT_FOUND,
        detail={"error": "refresh_run_not_found"},
    ) from error


@router.post(
    "",
    response_model=RefreshRunSnapshotResponse,
    dependencies=[Depends(require_refresh_auth)],
)
def start_run(body: RefreshRunStartBody) -> RefreshRunSnapshotResponse:
    try:
        snapshot = start_refresh_run(
            RefreshRunStartRequest(RunId(body.run_id), CycleId(body.cycle_id), body.force)
        )
    except (RefreshLeaseConflictError, RefreshRunIdempotencyError) as exc:
        _raise_conflict(exc)
    return _snapshot_response(snapshot)


@router.get(
    "/{run_id}",
    response_model=RefreshRunSnapshotResponse,
    dependencies=[Depends(require_refresh_auth)],
)
def read_run_status(
    run_id: Annotated[str, Path(min_length=1, max_length=128)],
) -> RefreshRunSnapshotResponse:
    try:
        return _snapshot_response(get_refresh_run(RunId(run_id)))
    except RefreshRunNotFoundError as exc:
        _raise_not_found(exc)


@router.post(
    "/{run_id}/advance",
    response_model=RefreshRunSnapshotResponse,
    dependencies=[Depends(require_refresh_auth)],
)
def advance_run(
    run_id: Annotated[str, Path(min_length=1, max_length=128)],
    body: RefreshRunAdvanceBody,
) -> RefreshRunSnapshotResponse:
    try:
        snapshot = advance_refresh_run(
            RefreshRunAdvanceRequest(RunId(run_id), WorkItemKey(body.expected_item_key))
        )
    except RefreshRunNotFoundError as exc:
        _raise_not_found(exc)
    except (RefreshLeaseConflictError, RefreshRunIdempotencyError) as exc:
        _raise_conflict(exc)
    return _snapshot_response(snapshot)


@router.post(
    "/{run_id}/finalize",
    response_model=RefreshRunSnapshotResponse,
    dependencies=[Depends(require_refresh_auth)],
)
def finalize_run(
    run_id: Annotated[str, Path(min_length=1, max_length=128)],
) -> RefreshRunSnapshotResponse:
    try:
        return _snapshot_response(finalize_refresh_run(RunId(run_id)))
    except RefreshRunNotFoundError as exc:
        _raise_not_found(exc)
