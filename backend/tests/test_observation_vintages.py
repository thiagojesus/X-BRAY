from pathlib import Path

import pytest


def test_red_xbr_003_migration_creates_append_only_vintage_ledger(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # Given
    import db.store as store

    if store._conn is not None:
        store._conn.close()
    store._conn = None


def test_sgs_vintage_is_idempotent_and_revision_is_append_only(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import db.store as store

    if store._conn is not None:
        store._conn.close()
    store._conn = None
    monkeypatch.setattr(store, "DB_PATH", tmp_path / "sgs-vintages.db")
    store.init_db()
    store.upsert_sgs(433, [{"data": "01/08/2026", "valor": "0.2"}])
    store.upsert_sgs(433, [{"data": "01/08/2026", "valor": "0.2"}])
    store.upsert_sgs(433, [{"data": "01/08/2026", "valor": "0.3"}])
    rows = store.query_observation_vintages("bcb_sgs", "ipca")
    assert len(rows) == 2
    assert [row["reference_period"] for row in rows] == ["2026-08-01", "2026-08-01"]
    assert all(row["source_published_at"] is None for row in rows)
    assert all(row["available_at"] == row["first_seen_at"] for row in rows)
    monkeypatch.setattr(store, "DB_PATH", tmp_path / "vintages.db")

    # When
    store.init_db()
    tables = {
        row["name"]
        for row in store._query("SELECT name FROM sqlite_master WHERE type = 'table'")
    }

    # Then
    assert "schema_migrations" in tables
    assert "observation_vintages" in tables

    if store._conn is not None:
        store._conn.close()
    store._conn = None
