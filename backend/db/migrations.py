from collections.abc import Sequence
from datetime import datetime, timezone
from types import TracebackType
from typing import Protocol, Self

from db.migration_steps import MIGRATIONS


class Result(Protocol):
    def fetchone(self) -> object | None: ...


class Cursor(Result, Protocol):
    def __enter__(self) -> Self: ...
    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc_value: BaseException | None,
        traceback: TracebackType | None,
    ) -> bool | None: ...
    def execute(self, query: str, params: Sequence[object] = ()) -> object: ...


class Connection(Protocol):
    def cursor(self) -> Cursor: ...
    def execute(self, query: str, params: Sequence[object] = ()) -> Result: ...
    def commit(self) -> None: ...


def run_migrations(connection: Connection, postgres: bool) -> None:
    create_schema_migrations = """CREATE TABLE IF NOT EXISTS schema_migrations (
        version TEXT PRIMARY KEY,
        applied_at TEXT NOT NULL
    )"""
    if postgres:
        with connection.cursor() as cursor:
            cursor.execute(create_schema_migrations)
            for name, _sqlite_statements, postgres_statements in MIGRATIONS:
                cursor.execute("SELECT version FROM schema_migrations WHERE version = %s", (name,))
                if cursor.fetchone() is not None:
                    continue
                for statement in postgres_statements:
                    cursor.execute(statement)
                cursor.execute(
                    "INSERT INTO schema_migrations (version, applied_at) VALUES (%s, %s)",
                    (name, datetime.now(timezone.utc).isoformat()),
                )
        connection.commit()
        return

    connection.execute(create_schema_migrations)
    for name, sqlite_statements, _postgres_statements in MIGRATIONS:
        row = connection.execute(
            "SELECT version FROM schema_migrations WHERE version = ?",
            (name,),
        ).fetchone()
        if row is not None:
            continue
        for statement in sqlite_statements:
            connection.execute(statement)
        connection.execute(
            "INSERT INTO schema_migrations (version, applied_at) VALUES (?, ?)",
            (name, datetime.now(timezone.utc).isoformat()),
        )
    connection.commit()
