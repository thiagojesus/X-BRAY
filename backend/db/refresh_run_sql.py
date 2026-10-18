import sqlite3
from collections.abc import Iterator, Sequence
from contextlib import AbstractContextManager, contextmanager
from datetime import datetime, timezone
from typing import Protocol, TypeAlias

from db import store
from db.refresh_run_models import (
    ClaimToken,
    CycleId,
    RefreshRunInputError,
    RefreshRunRecord,
    RefreshRunSnapshot,
    RefreshRunState,
    RefreshWorkItemRecord,
    RefreshWorkItemStatus,
    RunId,
    WorkItemKey,
)

SQLValue: TypeAlias = str | int | None


class Row(Protocol):
    def __getitem__(self, key: str, /) -> object: ...


class Cursor(Protocol):
    @property
    def rowcount(self) -> int: ...
    def fetchone(self) -> Row | None: ...
    def fetchall(self) -> Sequence[Row]: ...


class Connection(Protocol):
    def execute(self, query: str, params: Sequence[SQLValue] = ()) -> Cursor: ...


class PostgresConnection(Connection, Protocol):
    def commit(self) -> None: ...
    def transaction(self) -> AbstractContextManager[object]: ...


def utc_iso(value: str, field: str) -> str:
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as error:
        raise RefreshRunInputError(field, value) from error
    if parsed.tzinfo is None:
        raise RefreshRunInputError(field, value)
    return parsed.astimezone(timezone.utc).isoformat()


def is_postgres() -> bool:
    return store.uses_postgres()


@contextmanager
def transaction() -> Iterator[Connection]:
    if store.uses_postgres():
        connection: PostgresConnection = store._get_pg_conn()
        connection.commit()
        with connection.transaction():
            yield connection
        return
    with store._sqlite_lock:
        connection: sqlite3.Connection = store._get_conn()
        with connection:
            connection.execute("BEGIN IMMEDIATE")
            yield connection


@contextmanager
def read_transaction() -> Iterator[Connection]:
    if store.uses_postgres():
        connection: PostgresConnection = store._get_pg_conn()
        connection.commit()
        with connection.transaction():
            yield connection
        return
    with store._sqlite_lock:
        connection: sqlite3.Connection = store._get_conn()
        with connection:
            connection.execute("BEGIN")
            yield connection


def execute(connection: Connection, query: str, params: Sequence[SQLValue] = ()) -> Cursor:
    statement = query.replace("?", "%s") if store.uses_postgres() else query
    return connection.execute(statement, params)


def required_text(row: Row, field: str) -> str:
    value = row[field]
    if not isinstance(value, str):
        raise TypeError(f"database field {field} is not text")
    return value


def optional_text(row: Row, field: str) -> str | None:
    value = row[field]
    if value is None:
        return None
    if not isinstance(value, str):
        raise TypeError(f"database field {field} is not nullable text")
    return value


def required_int(row: Row, field: str) -> int:
    value = row[field]
    if isinstance(value, bool) or not isinstance(value, int):
        raise TypeError(f"database field {field} is not an integer")
    return value


def run_record(row: Row) -> RefreshRunRecord:
    return RefreshRunRecord(
        run_id=RunId(required_text(row, "run_id")),
        cycle_id=CycleId(required_text(row, "cycle_id")),
        state=RefreshRunState(required_text(row, "state")),
        force=bool(required_int(row, "force")),
        manifest_version=required_text(row, "manifest_version"),
        derived_cutoff=optional_text(row, "derived_cutoff"),
        macro_cutoff=optional_text(row, "macro_cutoff"),
        created_at=required_text(row, "created_at"),
        updated_at=required_text(row, "updated_at"),
        finished_at=optional_text(row, "finished_at"),
        final_result=optional_text(row, "final_result"),
    )


def item_record(row: Row) -> RefreshWorkItemRecord:
    token = optional_text(row, "claim_token")
    return RefreshWorkItemRecord(
        run_id=RunId(required_text(row, "run_id")),
        item_key=WorkItemKey(required_text(row, "item_key")),
        task=required_text(row, "task"),
        task_order=required_int(row, "task_order"),
        status=RefreshWorkItemStatus(required_text(row, "status")),
        attempt=required_int(row, "attempt"),
        claim_token=ClaimToken(token) if token is not None else None,
        claim_expires_at=optional_text(row, "claim_expires_at"),
        claimed_at=optional_text(row, "claimed_at"),
        created_at=required_text(row, "created_at"),
        updated_at=required_text(row, "updated_at"),
        finished_at=optional_text(row, "finished_at"),
        series_updated=required_int(row, "series_updated"),
        record_count=required_int(row, "record_count"),
        error_code=optional_text(row, "error_code"),
    )


def read_snapshot(connection: Connection, run_id: RunId) -> RefreshRunSnapshot | None:
    run_cursor = execute(connection, "SELECT * FROM refresh_runs WHERE run_id = ?", (run_id,))
    run_row = run_cursor.fetchone()
    if run_row is None:
        return None
    item_cursor = execute(
        connection,
        "SELECT * FROM refresh_work_items WHERE run_id = ? ORDER BY task_order",
        (run_id,),
    )
    lease_cursor = execute(
        connection,
        "SELECT expires_at FROM refresh_leases WHERE lock_key = ? AND owner = ?",
        ("daily_refresh", run_id),
    )
    lease_row = lease_cursor.fetchone()
    return RefreshRunSnapshot(
        run=run_record(run_row),
        items=tuple(item_record(row) for row in item_cursor.fetchall()),
        lease_expires_at=optional_text(lease_row, "expires_at") if lease_row else None,
    )
