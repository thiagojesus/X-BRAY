from typing import assert_never

from db.refresh_run_models import (
    MAX_WORK_ITEM_ATTEMPTS,
    RefreshLeaseConflictError,
    RefreshPersistenceError,
    RefreshRunCutoffOrderError,
    RefreshRunCutoffPhase,
    RefreshRunCutoffUpdate,
    RefreshRunFinalization,
    RefreshRunFinalState,
    RefreshRunState,
    RunId,
)
from db.refresh_run_sql import (
    Connection,
    Row,
    execute,
    is_postgres,
    optional_text,
    required_text,
    transaction,
    utc_iso,
)

_DAILY_REFRESH_LEASE = "daily_refresh"


def _lease_for_update(connection: Connection) -> Row | None:
    suffix = " FOR UPDATE" if is_postgres() else ""
    return execute(
        connection,
        f"SELECT owner, expires_at FROM refresh_leases WHERE lock_key = ?{suffix}",
        (_DAILY_REFRESH_LEASE,),
    ).fetchone()


def _run_for_update(connection: Connection, run_id: RunId) -> Row | None:
    suffix = " FOR UPDATE" if is_postgres() else ""
    return execute(
        connection,
        f"SELECT state, derived_cutoff, macro_cutoff FROM refresh_runs WHERE run_id = ?{suffix}",
        (run_id,),
    ).fetchone()


def persist_run_cutoff(update: RefreshRunCutoffUpdate) -> str:
    cutoff = utc_iso(update.cutoff, f"{update.phase.value}_cutoff")
    now = utc_iso(update.now, "now")
    lease_expires_at = utc_iso(update.lease_expires_at, "lease_expires_at")
    if lease_expires_at <= now:
        raise RefreshPersistenceError(update.run_id)
    with transaction() as connection:
        lease = _lease_for_update(connection)
        if lease is None:
            raise RefreshPersistenceError(update.run_id)
        lease_owner = RunId(required_text(lease, "owner"))
        lease_expiry = required_text(lease, "expires_at")
        if lease_owner != update.run_id or lease_expiry <= now:
            raise RefreshLeaseConflictError(update.run_id, lease_owner)
        run = _run_for_update(connection, update.run_id)
        if run is None or required_text(run, "state") != RefreshRunState.RUNNING.value:
            raise RefreshPersistenceError(update.run_id)
        derived_cutoff = optional_text(run, "derived_cutoff")
        match update.phase:
            case RefreshRunCutoffPhase.DERIVED:
                column = "derived_cutoff"
            case RefreshRunCutoffPhase.MACRO_SCORE:
                if derived_cutoff is None:
                    raise RefreshRunCutoffOrderError(update.run_id)
                if cutoff < derived_cutoff:
                    raise RefreshPersistenceError(update.run_id)
                column = "macro_cutoff"
            case unreachable:
                assert_never(unreachable)
        persisted = optional_text(run, column)
        if persisted is None:
            execute(
                connection,
                f"UPDATE refresh_runs SET {column} = ?, updated_at = ? "
                f"WHERE run_id = ? AND state = 'running' AND {column} IS NULL",
                (cutoff, now, update.run_id),
            )
            persisted = cutoff
        execute(
            connection,
            "UPDATE refresh_leases SET expires_at = CASE WHEN expires_at < ? "
            "THEN ? ELSE expires_at END WHERE lock_key = ? AND owner = ? AND expires_at > ?",
            (
                lease_expires_at,
                lease_expires_at,
                _DAILY_REFRESH_LEASE,
                update.run_id,
                now,
            ),
        )
        return persisted


def finalize_and_release_run(finalization: RefreshRunFinalization) -> bool:
    """Finalize only a fully resolved run and release its daily lease atomically."""
    finished_at = utc_iso(finalization.finished_at, "finished_at")
    last_successful_update = (
        utc_iso(finalization.last_successful_update, "last_successful_update")
        if finalization.last_successful_update is not None
        else None
    )
    with transaction() as connection:
        lease = _lease_for_update(connection)
        if (
            lease is None
            or required_text(lease, "owner") != finalization.run_id
            or required_text(lease, "expires_at") <= finished_at
        ):
            return False
        run = _run_for_update(connection, finalization.run_id)
        if run is None or required_text(run, "state") != RefreshRunState.RUNNING.value:
            return False
        unfinished = execute(
            connection,
            "SELECT item_key FROM refresh_work_items WHERE run_id = ? "
            "AND (status IN ('pending', 'claimed') OR (status = 'failed' AND attempt < ?)) LIMIT 1",
            (finalization.run_id, MAX_WORK_ITEM_ATTEMPTS),
        ).fetchone()
        if unfinished is not None:
            return False
        if finalization.state is RefreshRunFinalState.SUCCEEDED:
            failed = execute(
                connection,
                "SELECT item_key FROM refresh_work_items WHERE run_id = ? "
                "AND status <> 'succeeded' LIMIT 1",
                (finalization.run_id,),
            ).fetchone()
            if failed is not None:
                return False
        changed = execute(
            connection,
            "UPDATE refresh_runs SET state = ?, updated_at = ?, finished_at = ?, final_result = ? "
            "WHERE run_id = ? AND state = 'running'",
            (
                finalization.state.value,
                finished_at,
                finished_at,
                finalization.final_result,
                finalization.run_id,
            ),
        )
        if changed.rowcount != 1:
            return False
        if last_successful_update is not None:
            execute(
                connection,
                "INSERT INTO meta (key, value) VALUES (?, ?) ON CONFLICT (key) "
                "DO UPDATE SET value = excluded.value",
                ("last_successful_update", last_successful_update),
            )
        released = execute(
            connection,
            "DELETE FROM refresh_leases WHERE lock_key = ? AND owner = ?",
            (_DAILY_REFRESH_LEASE, finalization.run_id),
        )
        if released.rowcount != 1:
            raise RefreshPersistenceError(finalization.run_id)
        return True
