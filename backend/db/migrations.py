from collections.abc import Sequence
from datetime import datetime, timezone
from types import TracebackType
from typing import Protocol, Self

from db.migration_steps import MIGRATION_NAME, POSTGRES_STATEMENTS, SQLITE_STATEMENTS


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
    statements = POSTGRES_STATEMENTS if postgres else SQLITE_STATEMENTS
    if postgres:
        with connection.cursor() as cursor:
            cursor.execute(create_schema_migrations)
            cursor.execute("SELECT version FROM schema_migrations WHERE version = %s", (MIGRATION_NAME,))
            if cursor.fetchone() is None:
                for statement in statements:
                    cursor.execute(statement)
                cursor.execute(
                    "INSERT INTO schema_migrations (version, applied_at) VALUES (%s, %s)",
                    (MIGRATION_NAME, datetime.now(timezone.utc).isoformat()),
                )
        connection.commit()
        return

    connection.execute(create_schema_migrations)
    row = connection.execute(
        "SELECT version FROM schema_migrations WHERE version = ?",
        (MIGRATION_NAME,),
    ).fetchone()
    if row is None:
        for statement in statements:
            connection.execute(statement)
        connection.execute(
            "INSERT INTO schema_migrations (version, applied_at) VALUES (?, ?)",
            (MIGRATION_NAME, datetime.now(timezone.utc).isoformat()),
        )
    connection.commit()
