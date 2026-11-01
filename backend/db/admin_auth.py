"""Persistence operations for opaque human-admin sessions and OAuth flows."""

from __future__ import annotations

from collections.abc import Generator, Sequence
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from typing import Any, cast
from uuid import UUID

from db import store


@contextmanager
def _cursor(*, write: bool = False) -> Generator[Any, None, None]:
    if store.uses_postgres():
        import psycopg
        from psycopg.rows import dict_row

        connection = psycopg.connect(
            store.DATABASE_URL, row_factory=cast(Any, dict_row), connect_timeout=5
        )
        try:
            with connection.cursor() as cursor:
                yield cursor
            if write:
                connection.commit()
        except BaseException:
            connection.rollback()
            raise
        finally:
            connection.close()
        return

    with store._sqlite_lock:  # pyright: ignore[reportPrivateUsage]
        connection = store._get_conn()  # pyright: ignore[reportPrivateUsage]
        cursor = connection.cursor()
        try:
            if write:
                connection.execute("BEGIN IMMEDIATE")
            yield cursor
            if write:
                connection.commit()
        except BaseException:
            if write:
                connection.rollback()
            raise


def _sql(query: str) -> str:
    return query.replace("?", "%s") if store.uses_postgres() else query


def _row(row: Any) -> dict[str, Any] | None:
    if row is None:
        return None
    return dict(row)


def _now(now: datetime | None) -> datetime:
    value = now or datetime.now(timezone.utc)
    return value.astimezone(timezone.utc)


def _timestamp(value: datetime) -> str:
    return value.isoformat(timespec="microseconds")


def create_flow(
    *,
    flow_id: str,
    handle_hash: str,
    state_hash: str,
    binding_hash: str,
    ciphertext: bytes,
    nonce: bytes,
    key_version: str,
    callback_url: str,
    expires_at: datetime,
    now: datetime | None = None,
    max_per_hour: int = 5,
) -> bool:
    created = _timestamp(_now(now))
    with _cursor(write=True) as cursor:
        if store.uses_postgres():
            cursor.execute("SELECT pg_advisory_xact_lock(hashtextextended(%s, 0))", (binding_hash,))
        cursor.execute(
            _sql("SELECT count(*) AS flow_count FROM admin_oauth_flows WHERE browser_binding_hash = ? AND created_at >= ?"),
            (binding_hash, _timestamp(_now(now) - timedelta(hours=1))),
        )
        count_row = cursor.fetchone()
        count = int(cast(Any, count_row)["flow_count"] if isinstance(count_row, dict) else count_row[0])
        if count >= max_per_hour:
            return False
        cursor.execute(
            _sql("""INSERT INTO admin_oauth_flows
                (id, flow_handle_hash, state_hash, browser_binding_hash, verifier_ciphertext,
                 nonce, key_version, callback_url, created_at, expires_at, consumed_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, NULL)"""),
            (flow_id, handle_hash, state_hash, binding_hash, ciphertext, nonce, key_version,
             callback_url, created, _timestamp(expires_at)),
        )
        return True


def consume_flow(
    *, state_hash: str, handle_hash: str, binding_hash: str, now: datetime | None = None
) -> dict[str, Any] | None:
    current = _timestamp(_now(now))
    with _cursor(write=True) as cursor:
        cursor.execute(
            _sql("""UPDATE admin_oauth_flows SET consumed_at = ?
                WHERE state_hash = ? AND flow_handle_hash = ? AND browser_binding_hash = ?
                  AND consumed_at IS NULL AND expires_at > ?
                RETURNING id, verifier_ciphertext, nonce, key_version, callback_url"""),
            (current, state_hash, handle_hash, binding_hash, current),
        )
        return _row(cursor.fetchone())


def create_session(
    *, session_id: str, token_hash: str, user_id: str, csrf_hash: str,
    idle_expires_at: datetime, absolute_expires_at: datetime, now: datetime | None = None,
) -> None:
    current = _timestamp(_now(now))
    with _cursor(write=True) as cursor:
        cursor.execute(
            _sql("""INSERT INTO admin_sessions
                (id, session_token_hash, user_id, created_at, last_activity_at,
                 idle_expires_at, absolute_expires_at, revoked_at, csrf_token_hash)
                VALUES (?, ?, ?, ?, ?, ?, ?, NULL, ?)"""),
            (session_id, token_hash, user_id, current, current,
             _timestamp(idle_expires_at), _timestamp(absolute_expires_at), csrf_hash),
        )


def establish_session(
    *, session_id: str, token_hash: str, user_id: str, csrf_hash: str,
    idle_expires_at: datetime, absolute_expires_at: datetime,
    previous_token_hash: str | None, now: datetime | None = None,
) -> None:
    current = _timestamp(_now(now))
    with _cursor(write=True) as cursor:
        cursor.execute(
            _sql("""INSERT INTO admin_sessions
                (id, session_token_hash, user_id, created_at, last_activity_at,
                 idle_expires_at, absolute_expires_at, revoked_at, csrf_token_hash)
                VALUES (?, ?, ?, ?, ?, ?, ?, NULL, ?)"""),
            (session_id, token_hash, user_id, current, current,
             _timestamp(idle_expires_at), _timestamp(absolute_expires_at), csrf_hash),
        )
        if previous_token_hash is not None:
            cursor.execute(
                _sql("UPDATE admin_sessions SET revoked_at = ? WHERE session_token_hash = ? AND revoked_at IS NULL"),
                (current, previous_token_hash),
            )
        _insert_event(cursor, user_id, "login", "google", current)


def get_session(token_hash: str, *, now: datetime | None = None) -> dict[str, Any] | None:
    current = _timestamp(_now(now))
    with _cursor() as cursor:
        cursor.execute(
            _sql("""SELECT s.id, s.user_id, s.created_at, s.last_activity_at,
                       s.idle_expires_at, s.absolute_expires_at, s.csrf_token_hash,
                       m.user_id AS active_member
                FROM admin_sessions s LEFT JOIN admin_memberships m
                  ON m.user_id = s.user_id AND m.revoked_at IS NULL
                WHERE s.session_token_hash = ? AND s.revoked_at IS NULL
                  AND s.idle_expires_at > ? AND s.absolute_expires_at > ?"""),
            (token_hash, current, current),
        )
        return _row(cursor.fetchone())


def revoke_session(token_hash: str, *, now: datetime | None = None) -> bool:
    current = _timestamp(_now(now))
    with _cursor(write=True) as cursor:
        cursor.execute(
            _sql("UPDATE admin_sessions SET revoked_at = ? WHERE session_token_hash = ? AND revoked_at IS NULL"),
            (current, token_hash),
        )
        return cursor.rowcount == 1


def update_activity(token_hash: str, *, idle_seconds: int, now: datetime | None = None) -> bool:
    with _cursor(write=True) as cursor:
        if now is None and store.uses_postgres():
            cursor.execute(
                """UPDATE admin_sessions
                   SET last_activity_at = statement_timestamp(),
                       idle_expires_at = LEAST(
                           statement_timestamp() + (%s * INTERVAL '1 second'), absolute_expires_at
                       )
                   WHERE session_token_hash = %s AND revoked_at IS NULL
                     AND idle_expires_at > statement_timestamp()
                     AND absolute_expires_at > statement_timestamp()
                     AND last_activity_at <= statement_timestamp() - INTERVAL '60 seconds'""",
                (idle_seconds, token_hash),
            )
            return cursor.rowcount == 1

        if now is None:
            next_idle = "strftime('%Y-%m-%dT%H:%M:%f+00:00', 'now', printf('+%d seconds', ?))"
            current = "strftime('%Y-%m-%dT%H:%M:%f+00:00', 'now')"
            cursor.execute(
                f"""UPDATE admin_sessions SET last_activity_at = {current},
                    idle_expires_at = CASE
                        WHEN julianday(absolute_expires_at) < julianday({next_idle}) THEN absolute_expires_at
                        ELSE {next_idle} END
                    WHERE session_token_hash = ? AND revoked_at IS NULL
                      AND julianday(idle_expires_at) > julianday({current})
                      AND julianday(absolute_expires_at) > julianday({current})
                      AND julianday(last_activity_at) <= julianday({current}) - (60.0 / 86400.0)""",
                (idle_seconds, idle_seconds, token_hash),
            )
            return cursor.rowcount == 1

        current_dt = _now(now)
        current = _timestamp(current_dt)
        next_idle = _timestamp(current_dt + timedelta(seconds=idle_seconds))
        cursor.execute(
            _sql("""UPDATE admin_sessions SET last_activity_at = ?, idle_expires_at =
                CASE WHEN absolute_expires_at < ? THEN absolute_expires_at ELSE ? END
                WHERE session_token_hash = ? AND revoked_at IS NULL
                  AND idle_expires_at > ? AND absolute_expires_at > ?
                  AND last_activity_at <= ?"""),
            (current, next_idle, next_idle, token_hash, current, current,
             _timestamp(current_dt - timedelta(seconds=60))),
        )
        return cursor.rowcount == 1


def is_admin(user_id: str) -> bool:
    with _cursor() as cursor:
        cursor.execute(
            _sql("SELECT 1 FROM admin_memberships WHERE user_id = ? AND revoked_at IS NULL"),
            (user_id,),
        )
        return cursor.fetchone() is not None


def grant_admin(user_id: str, *, granted_by: str, reason: str, now: datetime | None = None) -> None:
    user_uuid, actor_uuid = UUID(user_id), UUID(granted_by)
    current = _timestamp(_now(now))
    with _cursor(write=True) as cursor:
        cursor.execute(
            _sql("""INSERT INTO admin_memberships(user_id, granted_at, granted_by, revoked_at, reason)
                VALUES (?, ?, ?, NULL, ?)
                ON CONFLICT(user_id) DO UPDATE SET granted_at = excluded.granted_at,
                    granted_by = excluded.granted_by, revoked_at = NULL, reason = excluded.reason"""),
            (str(user_uuid), current, str(actor_uuid), reason[:200]),
        )
        _insert_event(cursor, user_id, "membership_granted", reason[:200], current)


def revoke_admin(user_id: str, *, granted_by: str, reason: str, now: datetime | None = None) -> bool:
    user_uuid, actor_uuid = UUID(user_id), UUID(granted_by)
    current = _timestamp(_now(now))
    with _cursor(write=True) as cursor:
        cursor.execute(
            _sql("""UPDATE admin_memberships SET revoked_at = ?, granted_by = ?, reason = ?
                WHERE user_id = ? AND revoked_at IS NULL"""),
            (current, str(actor_uuid), reason[:200], str(user_uuid)),
        )
        changed = cursor.rowcount == 1
        if changed:
            cursor.execute(
                _sql("UPDATE admin_sessions SET revoked_at = ? WHERE user_id = ? AND revoked_at IS NULL"),
                (current, str(user_uuid)),
            )
            _insert_event(cursor, user_id, "membership_revoked", reason[:200], current)
        return changed


def _insert_event(cursor: Any, user_id: str | None, event: str, reason: str, created: str) -> None:
    cursor.execute(
        _sql("INSERT INTO admin_auth_events(id, user_id, event, created_at, reason) VALUES (?, ?, ?, ?, ?)"),
        (str(UUID(int=0)) if store.uses_postgres() and user_id is None else __import__("uuid").uuid4().hex,
         user_id, event, created, reason),
    )


def record_event(user_id: str | None, event: str, reason: str = "") -> None:
    with _cursor(write=True) as cursor:
        _insert_event(cursor, user_id, event, reason[:200], _timestamp(_now(None)))


def cleanup(*, now: datetime | None = None, batch_size: int = 200) -> dict[str, int]:
    if not 1 <= batch_size <= 1000:
        raise ValueError("batch_size must be between 1 and 1000")
    current_dt = _now(now)
    cutoff_flows = _timestamp(current_dt - timedelta(hours=24))
    cutoff_sessions = _timestamp(current_dt - timedelta(days=30))
    cutoff_events = _timestamp(current_dt - timedelta(days=180))
    deleted: dict[str, int] = {}
    with _cursor(write=True) as cursor:
        for table, where, cutoff in (
            ("admin_oauth_flows", "expires_at < ?", cutoff_flows),
            ("admin_sessions", "(absolute_expires_at < ? OR revoked_at < ?)", cutoff_sessions),
            ("admin_auth_events", "created_at < ?", cutoff_events),
        ):
            if table == "admin_sessions":
                query = f"DELETE FROM {table} WHERE id IN (SELECT id FROM {table} WHERE {where} LIMIT ?)"
                params: Sequence[object] = (cutoff, cutoff, batch_size)
            else:
                query = f"DELETE FROM {table} WHERE id IN (SELECT id FROM {table} WHERE {where} LIMIT ?)"
                params = (cutoff, batch_size)
            cursor.execute(_sql(query), params)
            deleted[table] = cursor.rowcount
    return deleted
