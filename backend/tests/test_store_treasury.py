from collections.abc import Iterator
from pathlib import Path
from types import ModuleType
import sys
from unittest.mock import MagicMock

import pytest


@pytest.fixture(autouse=True)
def _isolated_database(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    import db.store as store

    if store._conn is not None:
        store._conn.close()
    store._conn = None
    monkeypatch.setattr(store, "DB_PATH", tmp_path / "treasury.db")
    store.init_db()
    yield
    if store._conn is not None:
        store._conn.close()
    store._conn = None


def test_replace_treasury_rows_persists_only_valid_records() -> None:
    # Given
    from db.store import query_treasury_rows, replace_treasury_rows

    records = [
        {"_bond_code": 2, "Data Base": "02/09/2026", "Tipo Titulo": "Tesouro IPCA+"},
        {"_bond_code": 1, "Data Base": "01/09/2026", "Tipo Titulo": "Tesouro Prefixado"},
        {"_bond_code": "invalid", "Data Base": "02/09/2026", "Tipo Titulo": "CDB"},
        {"_bond_code": 3, "Data Base": "invalid", "Tipo Titulo": "Tesouro Prefixado"},
        {"_bond_code": 4, "Data Base": "", "Tipo Titulo": "Tesouro Prefixado"},
    ]

    # When
    count = replace_treasury_rows(records)

    # Then
    assert count == 2
    assert query_treasury_rows() == [
        {"Data Base": "01/09/2026", "Tipo Titulo": "Tesouro Prefixado"},
        {"Data Base": "02/09/2026", "Tipo Titulo": "Tesouro IPCA+"},
    ]


def test_replace_treasury_rows_returns_zero_without_writing() -> None:
    # Given
    from db.store import query_treasury_rows, replace_treasury_rows

    # When
    count = replace_treasury_rows([{"Data Base": "02/09/2026"}])

    # Then
    assert count == 0
    assert query_treasury_rows() == []


def test_replace_treasury_rows_replaces_previous_snapshot() -> None:
    # Given
    from db.store import query_treasury_rows, replace_treasury_rows

    replace_treasury_rows([{"_bond_code": 1, "Data Base": "01/09/2026", "name": "old"}])

    # When
    count = replace_treasury_rows([{"_bond_code": 2, "Data Base": "02/09/2026", "name": "new"}])

    # Then
    assert count == 1
    assert query_treasury_rows() == [{"Data Base": "02/09/2026", "name": "new"}]


def test_query_treasury_latest_rows_returns_only_latest_snapshot() -> None:
    # Given
    from db.store import query_treasury_latest_rows, replace_treasury_rows

    replace_treasury_rows([
        {"_bond_code": 1, "Data Base": "01/09/2026", "name": "old"},
        {"_bond_code": 2, "Data Base": "01/09/2026", "name": "also old"},
        {"_bond_code": 1, "Data Base": "02/09/2026", "name": "new"},
    ])

    # When
    rows = query_treasury_latest_rows()

    # Then
    assert rows == [{"Data Base": "02/09/2026", "name": "new"}]


def test_postgres_connection_disables_prepared_statements(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # Given
    import db.store as store

    class FakeConnection:
        pass

    connection = FakeConnection()
    psycopg = ModuleType("psycopg")
    connect = MagicMock(return_value=connection)
    setattr(psycopg, "connect", connect)
    rows = ModuleType("psycopg.rows")
    dict_row = object()
    setattr(rows, "dict_row", dict_row)
    monkeypatch.setitem(sys.modules, "psycopg", psycopg)
    monkeypatch.setitem(sys.modules, "psycopg.rows", rows)
    monkeypatch.setattr(store, "DATABASE_URL", "postgresql://example.test/db")
    monkeypatch.setattr(store._pg_local, "conn", None, raising=False)

    # When
    result = store._get_pg_conn()

    # Then
    assert result is connection
    connect.assert_called_once_with(
        "postgresql://example.test/db",
        prepare_threshold=None,
        row_factory=dict_row,
    )


def test_query_b3_di_supports_independent_date_bounds() -> None:
    # Given
    from db.store import query_b3_di, upsert_b3_di

    upsert_b3_di([
        {"trade_date": "2026-09-01", "symbol": "DI1F27", "maturity": "2027-01-04", "rate": 12.1},
        {"trade_date": "2026-09-02", "symbol": "DI1G27", "maturity": "2027-02-01", "rate": 12.2},
    ])

    # When
    from_start = query_b3_di(start_date="2026-09-02")
    through_end = query_b3_di(end_date="2026-09-01")

    # Then
    assert [row["trade_date"] for row in from_start] == ["2026-09-02"]
    assert [row["trade_date"] for row in through_end] == ["2026-09-01"]


def test_db_stats_counts_treasury_rows() -> None:
    # Given
    from db.store import db_stats, replace_treasury_rows

    replace_treasury_rows([{"_bond_code": 1, "Data Base": "01/09/2026", "name": "bond"}])

    # When
    stats = db_stats()

    # Then
    assert stats["treasury"] == 1
