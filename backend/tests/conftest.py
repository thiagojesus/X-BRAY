import os
import sqlite3
from collections.abc import Iterator
from pathlib import Path

import pytest

os.environ.setdefault("XBRAY_ENV", "test")


def _connection_is_open(connection: sqlite3.Connection | None) -> bool:
    if connection is None:
        return False
    try:
        connection.execute("SELECT 1")
    except sqlite3.ProgrammingError:
        return False
    return True


@pytest.fixture(autouse=True)
def isolated_sqlite_store(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> Iterator[Path]:
    """Route real SQLite persistence to a per-test file without owning foreign connections."""
    from db import store

    previous_connection = store._conn
    store._conn = None
    database_path = tmp_path / "isolated-xbry.db"
    monkeypatch.setattr(store, "DB_PATH", database_path)
    monkeypatch.setattr(store, "DATABASE_URL", "")
    owned_connections: list[sqlite3.Connection] = []
    try:
        store.init_db()
        if store._conn is None:
            raise RuntimeError("isolated SQLite initialization did not open a connection")
        owned_connections.append(store._conn)
        yield database_path
    finally:
        if store._conn is not None and store._conn is not previous_connection:
            owned_connections.append(store._conn)
        for connection in owned_connections:
            if connection is not previous_connection and _connection_is_open(connection):
                connection.close()
        store._conn = None
        if store._conn is None and _connection_is_open(previous_connection):
            store._conn = previous_connection
