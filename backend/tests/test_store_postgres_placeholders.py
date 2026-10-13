from collections.abc import Sequence
from types import TracebackType
from typing import Self

import pytest

SQLParam = str | int | float | bool | None


class FakeCursor:
    def __init__(self) -> None:
        self.executions: list[tuple[str, Sequence[SQLParam]]] = []

    def __enter__(self) -> Self:
        return self

    def __exit__(
        self,
        _exc_type: type[BaseException] | None,
        _exc_value: BaseException | None,
        _traceback: TracebackType | None,
    ) -> None:
        return None

    def execute(self, sql: str, params: Sequence[SQLParam]) -> None:
        self.executions.append((sql, params))


class FakeConnection:
    def __init__(self, cursor: FakeCursor) -> None:
        self._cursor = cursor

    def cursor(self) -> FakeCursor:
        return self._cursor

    def commit(self) -> None:
        return None


def _query_current_last_success(
    _sql: str,
    _params: list[SQLParam] | tuple[SQLParam, ...] = (),
) -> list[dict[str, str | None]]:
    return [{"last_success": "2026-10-12T10:00:00+00:00"}]


def test_postgres_refresh_finish_adapts_placeholders_before_psycopg_execute(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # Given
    from db import store

    cursor = FakeCursor()
    connection = FakeConnection(cursor)
    monkeypatch.setattr(store, "_is_pg", lambda: True)
    monkeypatch.setattr(store, "_get_pg_conn", lambda: connection)
    monkeypatch.setattr(store, "_query", _query_current_last_success)

    # When
    store.record_refresh_task_finish(
        task="sgs_juros",
        status="error",
        series_updated=0,
        record_count=0,
        duration_ms=125,
        error_code="ProviderTimeout",
        finished_at="2026-10-13T10:00:00+00:00",
    )

    # Then
    assert len(cursor.executions) == 1
    sql, params = cursor.executions[0]
    assert sql.count("%s") == 8
    assert "?" not in sql
    assert params == [
        "2026-10-13T10:00:00+00:00",
        "2026-10-12T10:00:00+00:00",
        "error",
        0,
        0,
        125,
        "ProviderTimeout",
        "sgs_juros",
    ]
