from datetime import datetime
from uuid import uuid4

from db.refresh_run_models import (
    MAX_WORK_ITEM_ATTEMPTS,
    ClaimToken,
    RefreshRunInputError,
    RefreshRunState,
    RefreshWorkItemClaimRequest,
    RefreshWorkItemCompletion,
    RefreshWorkItemDeferral,
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
    optional_text,
    required_int,
    required_text,
    transaction,
    utc_iso,
)
from refresh_run_scheduler import item_eligibility, source_scope_for_task

_DAILY_REFRESH_LEASE = "daily_refresh"


def _run_for_update(connection: Connection, run_id: RunId) -> Row | None:
    suffix = " FOR UPDATE" if is_postgres() else ""
    return execute(
        connection,
        f"SELECT state, manifest_version FROM refresh_runs WHERE run_id = ?{suffix}",
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
        version = required_text(run, "manifest_version")
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
                "claim_expires_at = NULL, updated_at = ?, finished_at = NULL, error_code = ? "
                "WHERE run_id = ? AND status = 'claimed' AND attempt >= ? AND claim_expires_at <= ?",
                (now, "AttemptLimitExceeded", request.run_id, MAX_WORK_ITEM_ATTEMPTS, now),
            )
            for task_row in exhausted:
                task = required_text(task_row, "task")
                execute(
                    connection,
                    "UPDATE refresh_task_status SET status = 'error', finished_at = NULL, error = ? "
                    "WHERE task = ? AND status = 'running'",
                    (None, task),
                )
        active = execute(
            connection,
            "SELECT item_key FROM refresh_work_items WHERE run_id = ? "
            "AND status = 'claimed' AND claim_expires_at > ? LIMIT 1",
            (request.run_id, now),
        ).fetchone()
        if active is not None:
            return None
        if version == "daily-refresh-v6":
            return _claim_v6_item(connection, request, now, expires_at)
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
            "claim_generation = claim_generation + 1, "
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


def _claim_v6_item(connection: Connection, request: RefreshWorkItemClaimRequest,
                   now: str, expires_at: str) -> RefreshWorkItemRecord | None:
    now_dt = datetime.fromisoformat(now)
    states = {
        required_text(row, "scope_id"): {
            key: row[key] for key in (
                "state", "opened_until", "retry_not_before", "probe_lease_until",
            )
        }
        for row in execute(
            connection,
            "SELECT scope_id, state, opened_until, retry_not_before, probe_lease_until "
            "FROM source_access_state",
        ).fetchall()
    }
    rows = execute(
        connection,
        "SELECT * FROM refresh_work_items WHERE run_id = ? ORDER BY task_order",
        (request.run_id,),
    ).fetchall()
    chosen: Row | None = None
    for row in rows:
        item_key = WorkItemKey(required_text(row, "item_key"))
        if request.expected_item_key is not None and item_key != request.expected_item_key:
            continue
        status = required_text(row, "status")
        acquisitions = required_int(row, "acquisition_attempts")
        retry_at = optional_text(row, "next_attempt_at")
        if acquisitions >= MAX_WORK_ITEM_ATTEMPTS:
            continue
        if retry_at is not None and retry_at > now:
            continue
        if status == "failed" and acquisitions >= MAX_WORK_ITEM_ATTEMPTS:
            continue
        if status == "claimed" and optional_text(row, "claim_expires_at") is not None \
                and optional_text(row, "claim_expires_at") > now:
            continue
        if status not in {"pending", "failed", "claimed"}:
            continue
        task = required_text(row, "task")
        eligible, _, _ = item_eligibility(task, states, now_dt)
        if not eligible:
            scope = source_scope_for_task(task)
            state = states.get(scope) if scope else None
            next_at = None if state is None else max(
                (value for value in (
                    optional_text(state, "retry_not_before"),
                    optional_text(state, "opened_until"),
                    optional_text(state, "probe_lease_until"),
                ) if value is not None), default=None,
            )
            if next_at is not None:
                execute(
                    connection,
                    "UPDATE refresh_work_items SET next_attempt_at = ?, deferral_reason = ?, "
                    "error_code = 'Deferred', updated_at = ? WHERE run_id = ? AND item_key = ? "
                    "AND status IN ('pending', 'failed')",
                    (next_at, "publisher_cooldown", now, request.run_id, item_key),
                )
            continue
        order = required_int(row, "task_order")
        blocked_earlier = False
        deferred_prerequisite: str | None = None
        for earlier in rows:
            if required_int(earlier, "task_order") >= order:
                break
            earlier_status = required_text(earlier, "status")
            if earlier_status in {"succeeded", "partial", "skipped"}:
                continue
            if earlier_status == "failed" and required_int(earlier, "attempt") >= MAX_WORK_ITEM_ATTEMPTS:
                continue
            earlier_retry = optional_text(earlier, "next_attempt_at")
            if earlier_retry is not None and earlier_retry > now:
                # Source work may overtake deferred siblings. Derived work may not.
                if task in {"derived_snapshots", "macro_score", "inflation_target_rules"}:
                    blocked_earlier = True
                    deferred_prerequisite = earlier_retry
                    break
                continue
            earlier_task = required_text(earlier, "task")
            earlier_eligible, _, _ = item_eligibility(earlier_task, states, now_dt)
            if earlier_status in {"pending", "failed", "claimed"} and earlier_eligible:
                blocked_earlier = True
                break
        if blocked_earlier:
            if deferred_prerequisite is not None and task in {
                "derived_snapshots", "macro_score", "inflation_target_rules",
            }:
                execute(
                    connection,
                    "UPDATE refresh_work_items SET next_attempt_at = ?, deferral_reason = ?, "
                    "error_code = 'Deferred', updated_at = ? WHERE run_id = ? AND item_key = ? "
                    "AND status = 'pending' AND deferral_reason IS NULL",
                    (deferred_prerequisite, "source_dependency_deferred", now, request.run_id, item_key),
                )
            continue
        chosen = row
        break
    if chosen is None:
        return None
    item_key = WorkItemKey(required_text(chosen, "item_key"))
    changed = execute(
        connection,
        "UPDATE refresh_work_items SET status = 'claimed', claim_generation = claim_generation + 1, "
        "attempt = attempt + 1, "
        "claim_token = ?, claim_expires_at = ?, claimed_at = ?, updated_at = ?, finished_at = NULL, "
        "series_updated = 0, record_count = 0, error_code = NULL, next_attempt_at = NULL, "
        "deferral_reason = NULL WHERE run_id = ? AND item_key = ? AND status IN ('pending', 'failed', 'claimed') "
        "AND acquisition_attempts < ? AND (next_attempt_at IS NULL OR next_attempt_at <= ?)",
        (str(uuid4()), expires_at, now, now, request.run_id, item_key, MAX_WORK_ITEM_ATTEMPTS, now),
    )
    if changed.rowcount != 1:
        return None
    record = execute(
        connection,
        "SELECT * FROM refresh_work_items WHERE run_id = ? AND item_key = ?",
        (request.run_id, item_key),
    ).fetchone()
    return None if record is None else item_record(record)


def defer_work_item_claim(request: RefreshWorkItemDeferral) -> bool:
    now = utc_iso(request.now, "now")
    retry_at = utc_iso(request.next_attempt_at, "next_attempt_at")
    if retry_at <= now or not request.reason:
        raise RefreshRunInputError("next_attempt_at", request.next_attempt_at)
    with transaction() as connection:
        changed = execute(
            connection,
            "UPDATE refresh_work_items SET status = 'pending', claim_token = NULL, "
            "claim_expires_at = NULL, claimed_at = NULL, updated_at = ?, finished_at = NULL, "
            "next_attempt_at = ?, deferral_reason = ?, error_code = 'Deferred', "
            "last_error_category = ?, last_error_code = ? "
            "WHERE run_id = ? AND item_key = ? AND status = 'claimed' AND claim_token = ? "
            "AND claim_expires_at > ? AND EXISTS (SELECT 1 FROM refresh_runs "
            "WHERE run_id = ? AND state = 'running') AND EXISTS (SELECT 1 FROM refresh_leases "
            "WHERE lock_key = ? AND owner = ? AND expires_at > ?)",
            (now, retry_at, request.reason[:80], request.error_category, request.error_code,
             request.run_id, request.item_key,
             request.claim_token, now, request.run_id, _DAILY_REFRESH_LEASE, request.run_id, now),
        )
        return changed.rowcount == 1


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
