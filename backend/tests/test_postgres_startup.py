from __future__ import annotations

import os
from collections.abc import Sequence
from concurrent.futures import ThreadPoolExecutor
from threading import Barrier, Event, local
from types import TracebackType
from typing import Self
from uuid import uuid4

import psycopg
import pytest
from psycopg import Cursor, Transaction, sql
from psycopg.conninfo import conninfo_to_dict, make_conninfo

from db import store
from db.migration_steps import MIGRATIONS
from db.migrations import POSTGRES_SCHEMA_LOCK_KEY


class _ObservedCursor:
    def __init__(self, cursor: Cursor, lock_attempt: Event) -> None:
        self._cursor = cursor
        self._lock_attempt = lock_attempt

    def __enter__(self) -> Self:
        self._cursor.__enter__()
        return self

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc_value: BaseException | None,
        traceback: TracebackType | None,
    ) -> bool | None:
        return self._cursor.__exit__(exc_type, exc_value, traceback)

    def execute(self, query: str, params: Sequence[str | int | None] = ()) -> None:
        if query == "SELECT pg_advisory_xact_lock(%s)":
            self._lock_attempt.set()
        self._cursor.execute(query, params)

    def fetchone(self) -> tuple[str | None, ...] | None:
        return self._cursor.fetchone()


class _ObservedConnection:
    def __init__(self, connection: psycopg.Connection, lock_attempt: Event) -> None:
        self._connection = connection
        self._lock_attempt = lock_attempt

    def cursor(self) -> _ObservedCursor:
        return _ObservedCursor(self._connection.cursor(), self._lock_attempt)

    def transaction(self) -> Transaction:
        return self._connection.transaction()

    def commit(self) -> None:
        self._connection.commit()

    def close(self) -> None:
        self._connection.close()


class _WorkerConnections(local):
    connection: _ObservedConnection | None

    def __init__(self) -> None:
        self.connection = None


def test_concurrent_postgres_init_db_serializes_cold_start_ddl(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # Given
    database_url = os.environ.get("TEST_DATABASE_URL")
    if not database_url:
        pytest.skip("requires TEST_DATABASE_URL for isolated PostgreSQL migration testing")
    database_name = conninfo_to_dict(database_url).get("dbname", "")
    if not database_name.endswith("_test"):
        pytest.fail("TEST_DATABASE_URL must point to a database named *_test")

    schema = f"xbray_startup_{uuid4().hex}"
    admin = psycopg.connect(database_url, autocommit=True)
    observer: psycopg.Connection | None = None
    lock_holder: psycopg.Connection | None = None
    schema_created = False
    try:
        with admin.cursor() as cursor:
            cursor.execute(sql.SQL("CREATE SCHEMA {}").format(sql.Identifier(schema)))
        schema_created = True
        worker_url = make_conninfo(database_url, options=f"-c search_path={schema}")
        observer = psycopg.connect(worker_url)
        lock_holder = psycopg.connect(worker_url)
        worker_connections = _WorkerConnections()
        lock_attempts = (Event(), Event())
        barrier = Barrier(2)

        def get_worker_connection() -> _ObservedConnection:
            assert worker_connections.connection is not None
            return worker_connections.connection

        def initialize(event: Event) -> None:
            raw_connection = psycopg.connect(worker_url)
            connection = _ObservedConnection(raw_connection, event)
            worker_connections.connection = connection
            try:
                barrier.wait(timeout=15)
                store.init_db()
            finally:
                connection.close()
                worker_connections.connection = None

        monkeypatch.setattr(store, "DATABASE_URL", database_url)
        monkeypatch.setattr(store, "_get_pg_conn", get_worker_connection)

        # When
        with ThreadPoolExecutor(max_workers=2) as executor:
            with lock_holder.transaction():
                with lock_holder.cursor() as cursor:
                    cursor.execute(
                        "SELECT pg_advisory_xact_lock(%s)",
                        (POSTGRES_SCHEMA_LOCK_KEY,),
                    )
                futures = [executor.submit(initialize, event) for event in lock_attempts]
                assert all(event.wait(timeout=10) for event in lock_attempts)
                with observer.cursor() as cursor:
                    cursor.execute(
                        "SELECT to_regclass('sgs'), to_regclass('schema_migrations'), "
                        "to_regclass('refresh_runs')"
                    )
                    assert cursor.fetchone() == (None, None, None)
                observer.commit()
            for future in futures:
                future.result(timeout=30)

        # Then
        with observer.cursor() as cursor:
            cursor.execute("SELECT version FROM schema_migrations ORDER BY version")
            versions = tuple(row[0] for row in cursor.fetchall())
            expected_versions = tuple(sorted(name for name, _sqlite, _postgres in MIGRATIONS))
            assert versions == expected_versions
            cursor.execute("SELECT to_regclass('refresh_runs'), to_regclass('refresh_work_items')")
            assert cursor.fetchone() == ("refresh_runs", "refresh_work_items")
    finally:
        if lock_holder is not None:
            lock_holder.close()
        if observer is not None:
            observer.close()
        if schema_created:
            with admin.cursor() as cursor:
                cursor.execute(sql.SQL("DROP SCHEMA {} CASCADE").format(sql.Identifier(schema)))
        admin.close()
