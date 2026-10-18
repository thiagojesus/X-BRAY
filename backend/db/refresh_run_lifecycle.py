from collections.abc import Sequence
from dataclasses import replace

from db.refresh_run_models import (
    RefreshLeaseConflictError,
    RefreshPersistenceError,
    RefreshRunIdempotencyError,
    RefreshRunInputError,
    RefreshRunSnapshot,
    RefreshRunStart,
    RefreshRunState,
    RefreshWorkItemSpec,
    RunId,
)
from db.refresh_run_sql import (
    Connection,
    execute,
    read_snapshot,
    read_transaction,
    required_text,
    transaction,
    utc_iso,
)

_DAILY_REFRESH_LEASE = "daily_refresh"


def _ordered_specs(items: Sequence[RefreshWorkItemSpec]) -> tuple[RefreshWorkItemSpec, ...]:
    return tuple(sorted(items, key=lambda item: item.task_order))


def _same_manifest(snapshot: RefreshRunSnapshot, start: RefreshRunStart) -> bool:
    current_items = tuple(
        RefreshWorkItemSpec(item.item_key, item.task, item.task_order) for item in snapshot.items
    )
    return (
        snapshot.run.cycle_id == start.cycle_id
        and snapshot.run.force == start.force
        and snapshot.run.manifest_version == start.manifest_version
        and (start.derived_cutoff is None or snapshot.run.derived_cutoff == start.derived_cutoff)
        and (start.macro_cutoff is None or snapshot.run.macro_cutoff == start.macro_cutoff)
        and current_items == _ordered_specs(start.items)
    )


def _acquire_lease(connection: Connection, start: RefreshRunStart) -> bool:
    cursor = execute(
        connection,
        "INSERT INTO refresh_leases (lock_key, owner, expires_at) VALUES (?, ?, ?) "
        "ON CONFLICT (lock_key) DO UPDATE SET owner = excluded.owner, expires_at = "
        "CASE WHEN refresh_leases.expires_at < excluded.expires_at "
        "THEN excluded.expires_at ELSE refresh_leases.expires_at END "
        "WHERE refresh_leases.owner = excluded.owner OR refresh_leases.expires_at <= ?",
        (_DAILY_REFRESH_LEASE, start.run_id, start.lease_expires_at, start.now),
    )
    return cursor.rowcount == 1


def _lease_owner(connection: Connection, run_id: RunId) -> RunId:
    row = execute(
        connection,
        "SELECT owner FROM refresh_leases WHERE lock_key = ?",
        (_DAILY_REFRESH_LEASE,),
    ).fetchone()
    if row is None:
        raise RefreshPersistenceError(run_id)
    return RunId(required_text(row, "owner"))


def _insert_run(connection: Connection, start: RefreshRunStart) -> None:
    execute(
        connection,
        "INSERT INTO refresh_runs "
        "(run_id, cycle_id, state, force, manifest_version, derived_cutoff, macro_cutoff, "
        "created_at, updated_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (
            start.run_id,
            start.cycle_id,
            RefreshRunState.RUNNING.value,
            int(start.force),
            start.manifest_version,
            start.derived_cutoff,
            start.macro_cutoff,
            start.now,
            start.now,
        ),
    )
    for item in _ordered_specs(start.items):
        execute(
            connection,
            "INSERT INTO refresh_work_items "
            "(run_id, item_key, task, task_order, status, created_at, updated_at) "
            "VALUES (?, ?, ?, ?, 'pending', ?, ?)",
            (start.run_id, item.item_key, item.task, item.task_order, start.now, start.now),
        )


def _abandon_expired_run(connection: Connection, owner: RunId, now: str, new_owner: RunId) -> None:
    run = execute(
        connection,
        "SELECT state FROM refresh_runs WHERE run_id = ?",
        (owner,),
    ).fetchone()
    if run is None or required_text(run, "state") != RefreshRunState.RUNNING.value:
        return
    active_claim = execute(
        connection,
        "SELECT item_key FROM refresh_work_items WHERE run_id = ? AND status = 'claimed' "
        "AND claim_expires_at > ? LIMIT 1",
        (owner, now),
    ).fetchone()
    if active_claim is not None:
        raise RefreshLeaseConflictError(new_owner, owner)
    tasks = execute(
        connection,
        "SELECT DISTINCT task FROM refresh_work_items WHERE run_id = ? "
        "AND status IN ('pending', 'claimed')",
        (owner,),
    ).fetchall()
    execute(
        connection,
        "UPDATE refresh_work_items SET status = 'failed', claim_token = NULL, "
        "claim_expires_at = NULL, updated_at = ?, finished_at = ?, error_code = ? "
        "WHERE run_id = ? AND status IN ('pending', 'claimed')",
        (now, now, "RefreshLeaseExpired", owner),
    )
    for task_row in tasks:
        task = required_text(task_row, "task")
        execute(
            connection,
            "UPDATE refresh_task_status SET status = 'error', finished_at = ?, error = ? "
            "WHERE task = ? AND status = 'running'",
            (now, "RefreshLeaseExpired", task),
        )
    execute(
        connection,
        "UPDATE refresh_runs SET state = 'failed', updated_at = ?, finished_at = ?, "
        "final_result = ? WHERE run_id = ? AND state = 'running'",
        (now, now, '{"status":"error","error_code":"RefreshLeaseExpired"}', owner),
    )


def create_or_resume_run(start: RefreshRunStart) -> RefreshRunSnapshot:
    """Create an idempotent run or renew its daily lease and return persisted state."""
    now = utc_iso(start.now, "now")
    expires_at = utc_iso(start.lease_expires_at, "lease_expires_at")
    derived = utc_iso(start.derived_cutoff, "derived_cutoff") if start.derived_cutoff else None
    macro = utc_iso(start.macro_cutoff, "macro_cutoff") if start.macro_cutoff else None
    normalized = replace(
        start,
        now=now,
        lease_expires_at=expires_at,
        derived_cutoff=derived,
        macro_cutoff=macro,
    )
    if expires_at <= now:
        raise RefreshRunInputError("lease_expires_at", start.lease_expires_at)

    with transaction() as connection:
        existing = read_snapshot(connection, start.run_id)
        if existing is not None and existing.run.state is not RefreshRunState.RUNNING:
            if not _same_manifest(existing, normalized):
                raise RefreshRunIdempotencyError(start.run_id)
            return existing
        previous_lease = execute(
            connection,
            "SELECT owner, expires_at FROM refresh_leases WHERE lock_key = ?",
            (_DAILY_REFRESH_LEASE,),
        ).fetchone()
        if previous_lease is not None:
            previous_owner = RunId(required_text(previous_lease, "owner"))
            previous_expires_at = required_text(previous_lease, "expires_at")
            if previous_owner != start.run_id and previous_expires_at <= now:
                _abandon_expired_run(connection, previous_owner, now, start.run_id)
        if not _acquire_lease(connection, normalized):
            raise RefreshLeaseConflictError(start.run_id, _lease_owner(connection, start.run_id))
        existing = read_snapshot(connection, start.run_id)
        if existing is not None:
            if not _same_manifest(existing, normalized):
                raise RefreshRunIdempotencyError(start.run_id)
            if existing.run.state is not RefreshRunState.RUNNING:
                execute(
                    connection,
                    "DELETE FROM refresh_leases WHERE lock_key = ? AND owner = ?",
                    (_DAILY_REFRESH_LEASE, start.run_id),
                )
            return existing
        _insert_run(connection, normalized)
        snapshot = read_snapshot(connection, start.run_id)
        if snapshot is None:
            raise RefreshPersistenceError(start.run_id)
        return snapshot


def read_run(run_id: RunId) -> RefreshRunSnapshot | None:
    with read_transaction() as connection:
        return read_snapshot(connection, run_id)
