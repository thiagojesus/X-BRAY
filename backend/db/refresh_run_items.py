from uuid import uuid4

from db.refresh_run_models import (
    MAX_WORK_ITEM_ATTEMPTS,
    ClaimToken,
    RefreshRunInputError,
    RefreshRunState,
    RefreshWorkItemClaimRequest,
    RefreshWorkItemCompletion,
    RefreshWorkItemRecord,
    RunId,
    WorkItemKey,
)
from db.refresh_run_sql import (
    Connection,
    Row,
    execute,
    is_postgres,
    item_record,
    required_int,
    required_text,
    transaction,
    utc_iso,
)

_DAILY_REFRESH_LEASE = "daily_refresh"


def _run_for_update(connection: Connection, run_id: RunId) -> Row | None:
    suffix = " FOR UPDATE" if is_postgres() else ""
    return execute(
        connection,
        f"SELECT state FROM refresh_runs WHERE run_id = ?{suffix}",
        (run_id,),
    ).fetchone()


def _has_live_lease(connection: Connection, run_id: RunId, now: str) -> bool:
    row = execute(
        connection,
        "SELECT owner, expires_at FROM refresh_leases WHERE lock_key = ?",
        (_DAILY_REFRESH_LEASE,),
    ).fetchone()
    return row is not None and required_text(row, "owner") == run_id and required_text(row, "expires_at") > now


def claim_next_work_item(request: RefreshWorkItemClaimRequest) -> RefreshWorkItemRecord | None:
    """Claim the earliest eligible item, rotating its token on every attempt."""
    now = utc_iso(request.now, "now")
    expires_at = utc_iso(request.claim_expires_at, "claim_expires_at")
    if expires_at <= now:
        raise RefreshRunInputError("claim_expires_at", request.claim_expires_at)
    with transaction() as connection:
        run = _run_for_update(connection, request.run_id)
        if run is None or required_text(run, "state") != RefreshRunState.RUNNING.value:
            return None
        if not _has_live_lease(connection, request.run_id, now):
            return None
        exhausted = execute(
            connection,
            "SELECT DISTINCT task FROM refresh_work_items WHERE run_id = ? AND status = 'claimed' "
            "AND attempt >= ? AND claim_expires_at <= ?",
            (request.run_id, MAX_WORK_ITEM_ATTEMPTS, now),
        ).fetchall()
        if exhausted:
            execute(
                connection,
                "UPDATE refresh_work_items SET status = 'failed', claim_token = NULL, "
                "claim_expires_at = NULL, updated_at = ?, finished_at = ?, error_code = ? "
                "WHERE run_id = ? AND status = 'claimed' AND attempt >= ? AND claim_expires_at <= ?",
                (now, now, "AttemptLimitExceeded", request.run_id, MAX_WORK_ITEM_ATTEMPTS, now),
            )
            for task_row in exhausted:
                task = required_text(task_row, "task")
                execute(
                    connection,
                    "UPDATE refresh_task_status SET status = 'error', finished_at = ?, error = ? "
                    "WHERE task = ? AND status = 'running'",
                    (now, "AttemptLimitExceeded", task),
                )
        active = execute(
            connection,
            "SELECT item_key FROM refresh_work_items WHERE run_id = ? "
            "AND status = 'claimed' AND claim_expires_at > ? LIMIT 1",
            (request.run_id, now),
        ).fetchone()
        if active is not None:
            return None
        if request.expected_item_key is not None:
            item = execute(
                connection,
                "SELECT task_order FROM refresh_work_items WHERE run_id = ? AND item_key = ?",
                (request.run_id, request.expected_item_key),
            ).fetchone()
            if item is None:
                return None
            earlier = execute(
                connection,
                "SELECT item_key FROM refresh_work_items WHERE run_id = ? AND task_order < ? "
                "AND (status = 'pending' OR (status = 'failed' AND attempt < ?) "
                "OR (status = 'claimed' AND attempt < ? AND claim_expires_at <= ?)) LIMIT 1",
                (
                    request.run_id,
                    required_int(item, "task_order"),
                    MAX_WORK_ITEM_ATTEMPTS,
                    MAX_WORK_ITEM_ATTEMPTS,
                    now,
                ),
            ).fetchone()
            if earlier is not None:
                return None
        suffix = " FOR UPDATE" if is_postgres() else ""
        expected_filter = " AND item_key = ?" if request.expected_item_key is not None else ""
        expected_params = (request.expected_item_key,) if request.expected_item_key is not None else ()
        eligible = execute(
            connection,
            "SELECT item_key FROM refresh_work_items WHERE run_id = ? AND "
            "(status = 'pending' OR (status = 'failed' AND attempt < ?) "
            "OR (status = 'claimed' AND attempt < ? AND claim_expires_at <= ?)) "
            f"{expected_filter} ORDER BY task_order LIMIT 1{suffix}",
            (
                request.run_id,
                MAX_WORK_ITEM_ATTEMPTS,
                MAX_WORK_ITEM_ATTEMPTS,
                now,
                *expected_params,
            ),
        ).fetchone()
        if eligible is None:
            return None
        item_key = WorkItemKey(required_text(eligible, "item_key"))
        token = ClaimToken(str(uuid4()))
        changed = execute(
            connection,
            "UPDATE refresh_work_items SET status = 'claimed', attempt = attempt + 1, "
            "claim_token = ?, claim_expires_at = ?, claimed_at = ?, updated_at = ?, "
            "finished_at = NULL, series_updated = 0, record_count = 0, error_code = NULL "
            "WHERE run_id = ? AND item_key = ?",
            (token, expires_at, now, now, request.run_id, item_key),
        )
        if changed.rowcount != 1:
            return None
        record = execute(
            connection,
            "SELECT * FROM refresh_work_items WHERE run_id = ? AND item_key = ?",
            (request.run_id, item_key),
        ).fetchone()
        return None if record is None else item_record(record)


def complete_work_item_claim(completion: RefreshWorkItemCompletion) -> bool:
    """Persist a result only while its claim token and run lease remain current."""
    finished_at = utc_iso(completion.finished_at, "finished_at")
    with transaction() as connection:
        changed = execute(
            connection,
            "UPDATE refresh_work_items SET status = ?, "
            "updated_at = ?, finished_at = ?, series_updated = ?, record_count = ?, error_code = ? "
            "WHERE run_id = ? AND item_key = ? AND status = 'claimed' AND claim_token = ? "
            "AND claim_expires_at > ? AND EXISTS (SELECT 1 FROM refresh_runs "
            "WHERE run_id = ? AND state = 'running') AND EXISTS (SELECT 1 FROM refresh_leases "
            "WHERE lock_key = ? AND owner = ? AND expires_at > ?)",
            (
                completion.status.value,
                finished_at,
                finished_at,
                completion.series_updated,
                completion.record_count,
                completion.error_code,
                completion.run_id,
                completion.item_key,
                completion.claim_token,
                finished_at,
                completion.run_id,
                _DAILY_REFRESH_LEASE,
                completion.run_id,
                finished_at,
            ),
        )
        return changed.rowcount == 1
