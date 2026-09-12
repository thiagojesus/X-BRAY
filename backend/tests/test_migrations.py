from pathlib import Path

import pytest


def test_sqlite_migrations_are_idempotent(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    import db.store as store

    if store._conn is not None:
        store._conn.close()
    store._conn = None
    monkeypatch.setattr(store, "DB_PATH", tmp_path / "migrations.db")
    store.init_db()
    store.init_db()
    versions = store._query("SELECT version FROM schema_migrations ORDER BY version")
    assert versions == [{"version": "001_observability"}]


def test_expired_lease_can_be_reclaimed_but_live_lease_cannot(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import db.store as store

    if store._conn is not None:
        store._conn.close()
    store._conn = None
    monkeypatch.setattr(store, "DB_PATH", tmp_path / "leases.db")
    store.init_db()
    assert store.acquire_refresh_lease("refresh", "owner-a", "2026-09-12T12:30:00+00:00", "2026-09-12T12:00:00+00:00")
    assert not store.acquire_refresh_lease("refresh", "owner-b", "2026-09-12T12:40:00+00:00", "2026-09-12T12:10:00+00:00")
    assert store.acquire_refresh_lease("refresh", "owner-b", "2026-09-12T13:10:00+00:00", "2026-09-12T13:00:00+00:00")
    assert not store.release_refresh_lease("refresh", "owner-a")
    assert store.release_refresh_lease("refresh", "owner-b")
