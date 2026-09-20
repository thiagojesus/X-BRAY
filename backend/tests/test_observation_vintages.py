from pathlib import Path

import pytest


def _reset_store(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, name: str):
    import db.store as store

    if store._conn is not None:
        store._conn.close()
    store._conn = None
    monkeypatch.setattr(store, "DB_PATH", tmp_path / name)
    monkeypatch.setattr(store, "DATABASE_URL", "")
    store.init_db()
    return store


def _close_store(store) -> None:
    if store._conn is not None:
        store._conn.close()
    store._conn = None


def test_red_xbr_003_migration_creates_append_only_vintage_ledger(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    store = _reset_store(tmp_path, monkeypatch, "vintages.db")
    try:
        tables = {
            row["name"]
            for row in store._query("SELECT name FROM sqlite_master WHERE type = 'table'")
        }
        assert "schema_migrations" in tables
        assert "observation_vintages" in tables
    finally:
        _close_store(store)


def test_sgs_vintage_is_idempotent_and_revision_is_append_only(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    store = _reset_store(tmp_path, monkeypatch, "sgs-vintages.db")
    try:
        store.upsert_sgs(433, [{"data": "01/08/2026", "valor": "0.2"}])
        store.upsert_sgs(433, [{"data": "01/08/2026", "valor": "0.2"}])
        store.upsert_sgs(433, [{"data": "01/08/2026", "valor": "0.3"}])

        rows = store.query_observation_vintages("bcb_sgs", "ipca")
        assert len(rows) == 2
        assert [row["reference_period"] for row in rows] == ["2026-08-01", "2026-08-01"]
        assert all(row["source_published_at"] is None for row in rows)
        assert all(row["available_at"] == row["first_seen_at"] for row in rows)
    finally:
        _close_store(store)


def test_red_xbr_003_invalid_storage_records_are_rejected(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    store = _reset_store(tmp_path, monkeypatch, "invalid-records.db")
    try:
        store.upsert_sgs(433, [{"data": "31/02/2026", "valor": "NaN"}])
        store.upsert_focus("IPCA", [{"Data": "2026-02-31", "Mediana": float("nan")}])
        store.upsert_b3_di([
            {
                "trade_date": "not-a-date",
                "symbol": "DI1Z99",
                "maturity": "2099-12-01",
                "rate": float("nan"),
            }
        ])

        assert store._query("SELECT series_code, date, value FROM sgs") == []
        assert store._query("SELECT indicator, date, data_json FROM focus") == []
        assert store._query("SELECT trade_date, symbol, maturity, rate FROM b3_di") == []
        assert store._query("SELECT provider FROM observation_vintages") == []
    finally:
        _close_store(store)
