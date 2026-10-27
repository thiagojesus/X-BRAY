from __future__ import annotations

from collections.abc import Sequence
from pathlib import Path
from types import TracebackType
from typing import Self

import pytest


class _MigrationProbeError(Exception):
    pass


class _RecordingCursor:
    def __init__(self, events: list[str], fail_on: str | None) -> None:
        self.events = events
        self.fail_on = fail_on

    def __enter__(self) -> Self:
        return self

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc_value: BaseException | None,
        traceback: TracebackType | None,
    ) -> bool:
        del exc_value, traceback
        return False

    def execute(self, query: str, params: Sequence[object] = ()) -> None:
        del params
        self.events.append(query)
        if query == self.fail_on:
            raise _MigrationProbeError("simulated schema DDL failure")

    def fetchone(self) -> None:
        return None


class _RecordingTransaction:
    def __init__(self, connection: _RecordingConnection) -> None:
        self.connection = connection

    def __enter__(self) -> None:
        self.connection.events.append("BEGIN")

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc_value: BaseException | None,
        traceback: TracebackType | None,
    ) -> bool:
        del exc_value, traceback
        if exc_type is None:
            self.connection.commit()
        else:
            self.connection.rollback()
        return False


class _RecordingConnection:
    def __init__(self, fail_on: str | None = None) -> None:
        self.events: list[str] = []
        self.fail_on = fail_on

    def cursor(self) -> _RecordingCursor:
        return _RecordingCursor(self.events, self.fail_on)

    def transaction(self) -> _RecordingTransaction:
        return _RecordingTransaction(self)

    def commit(self) -> None:
        self.events.append("COMMIT")

    def rollback(self) -> None:
        self.events.append("ROLLBACK")


def test_sqlite_migrations_are_idempotent(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from db import store

    if store._conn is not None:
        store._conn.close()
    store._conn = None
    monkeypatch.setattr(store, "DB_PATH", tmp_path / "migrations.db")
    store.init_db()
    store.init_db()
    versions = store._query("SELECT version FROM schema_migrations ORDER BY version")
    assert versions == [
        {"version": "001_observability"},
        {"version": "002_sidra_observations"},
        {"version": "003_derived_snapshots"},
        {"version": "004_macro_score_snapshots"},
        {"version": "005_resumable_refresh"},
        {"version": "006_retrospective_macro_history"},
        {"version": "007_retrospective_di"},
        {"version": "008_di_maturity_provenance"},
        {"version": "009_b3_instrument_snapshots"},
        {"version": "010_production_data_imports"},
        {"version": "011_private_import_staging"},
        {"version": "012_import_validation_rows"},
        {"version": "013_source_extractions"},
        {"version": "014_tr_period_observations"},
        {"version": "015_ingestion_policy"},
        {"version": "016_candidate_acquisition"},
        {"version": "017_treasury_cohort_receipts"},
        {"version": "018_refresh_attempt_lifecycle"},
    ]


def test_expired_lease_can_be_reclaimed_but_live_lease_cannot(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from db import store

    if store._conn is not None:
        store._conn.close()
    store._conn = None
    monkeypatch.setattr(store, "DB_PATH", tmp_path / "leases.db")
    store.init_db()
    assert store.acquire_refresh_lease(
        "refresh", "owner-a", "2026-09-12T12:30:00+00:00", "2026-09-12T12:00:00+00:00"
    )
    assert not store.acquire_refresh_lease(
        "refresh", "owner-b", "2026-09-12T12:40:00+00:00", "2026-09-12T12:10:00+00:00"
    )
    assert store.acquire_refresh_lease(
        "refresh", "owner-b", "2026-09-12T13:10:00+00:00", "2026-09-12T13:00:00+00:00"
    )
    assert not store.release_refresh_lease("refresh", "owner-a")
    assert store.release_refresh_lease("refresh", "owner-b")


def test_tr_period_storage_keeps_full_identity_and_legacy_rows_untouched(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from db import store

    if store._conn is not None:
        store._conn.close()
    store._conn = None
    monkeypatch.setattr(store, "DB_PATH", tmp_path / "tr-periods.db")
    store.init_db()
    store.upsert_sgs(226, [{"data": "01/03/2015", "valor": "0.0661"}])
    periods = [
        {"data": "01/03/2015", "dataFim": "29/03/2015", "valor": "0.0661"},
        {"data": "01/03/2015", "dataFim": "30/03/2015", "valor": "0.0661"},
        {"data": "01/03/2015", "dataFim": "31/03/2015", "valor": "0.0978"},
    ]
    store.upsert_tr_periods(periods)
    assert store.query_tr_periods("01/03/2015", "01/03/2015") == periods
    assert store.query_sgs(226, "01/03/2015", "01/03/2015") == [
        {"data": "01/03/2015", "valor": "0.0661"}
    ]
    assert store.upsert_tr_periods([{"data": "01/03/2015", "dataFim": "29/03/2015", "valor": "9"}]) == 1
    assert len(store._query("SELECT 1 FROM tr_period_observations")) == 4


def test_tr_period_revisions_are_append_only_and_queryable_as_known_at(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from datetime import datetime, timezone

    from db import store

    if store._conn is not None:
        store._conn.close()
    store._conn = None
    monkeypatch.setattr(store, "DB_PATH", tmp_path / "tr-period-revisions.db")
    store.init_db()
    captures = iter((
        "2026-01-01T00:00:00+00:00",
        "2026-02-01T00:00:00+00:00",
        "2026-03-01T00:00:00+00:00",
    ))
    monkeypatch.setattr(store, "_utc_now", lambda: next(captures))
    original = {"data": "01/03/2015", "dataFim": "31/03/2015", "valor": "0.1"}
    revised = {**original, "valor": "0.2"}

    assert store.upsert_tr_periods([original]) == 1
    assert store.upsert_tr_periods([revised]) == 1
    assert store.upsert_tr_periods([revised]) == 0
    assert store.query_tr_periods(known_at="2026-01-15T00:00:00+00:00") == [original]
    assert store.query_tr_periods(known_at="2026-03-01T00:00:00+00:00") == [revised]
    revisions = store._query(
        "SELECT value, first_seen_at, payload_hash FROM tr_period_observations ORDER BY first_seen_at"
    )
    assert [row["value"] for row in revisions] == [0.1, 0.2]
    assert len({row["payload_hash"] for row in revisions}) == 2
    assert [row["first_seen_at"] for row in revisions] == [
        "2026-01-01T00:00:00+00:00", "2026-02-01T00:00:00+00:00"
    ]
    assert datetime.fromisoformat(revisions[0]["first_seen_at"]).tzinfo == timezone.utc
    monkeypatch.setattr(store, "_utc_now", lambda: "2026-02-01T00:00:00+00:00")
    with pytest.raises(ValueError, match="ambiguous_tr_period_revision_capture_time"):
        store.upsert_tr_periods([{**original, "valor": "0.3"}])
    assert len(store._query("SELECT 1 FROM tr_period_observations")) == 2
    with pytest.raises(ValueError, match="conflicting_tr_period_values_in_capture"):
        store.upsert_tr_periods([
            {"data": "02/03/2015", "dataFim": "01/04/2015", "valor": "0.4"},
            {"data": "02/03/2015", "dataFim": "01/04/2015", "valor": "0.5"},
        ])
    assert len(store._query("SELECT 1 FROM tr_period_observations")) == 2


def test_postgres_init_db_locks_schema_and_migrations_in_one_transaction(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # Given
    from db import store

    connection = _RecordingConnection()
    monkeypatch.setattr(store, "DATABASE_URL", "postgresql://local-test")
    monkeypatch.setattr(store, "_get_pg_conn", lambda: connection)
    monkeypatch.setattr(store, "PG_SCHEMA", "CREATE BASE SCHEMA")

    # When
    store.init_db()

    # Then
    advisory_lock = next(
        index
        for index, statement in enumerate(connection.events)
        if "pg_advisory_xact_lock" in statement
    )
    base_schema = connection.events.index("CREATE BASE SCHEMA")
    migration_table = connection.events.index(
        next(
            statement
            for statement in connection.events
            if "CREATE TABLE IF NOT EXISTS schema_migrations" in statement
        )
    )
    refresh_run_table = next(
        index
        for index, statement in enumerate(connection.events)
        if "CREATE TABLE IF NOT EXISTS refresh_runs" in statement
    )
    commit = connection.events.index("COMMIT", advisory_lock)
    assert advisory_lock < base_schema < migration_table < refresh_run_table < commit


def test_postgres_init_db_rolls_back_and_propagates_schema_failure(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # Given
    from db import store

    connection = _RecordingConnection(fail_on="CREATE BASE SCHEMA")
    monkeypatch.setattr(store, "DATABASE_URL", "postgresql://local-test")
    monkeypatch.setattr(store, "_get_pg_conn", lambda: connection)
    monkeypatch.setattr(store, "PG_SCHEMA", "CREATE BASE SCHEMA")

    # When / Then
    with pytest.raises(_MigrationProbeError):
        store.init_db()
    assert connection.events[-1] == "ROLLBACK"


def test_red_xbr_003_ci_executes_postgres_smoke() -> None:
    workflow = (Path(__file__).parents[2] / ".github/workflows/ci.yml").read_text(
        encoding="utf-8"
    )

    assert "run: python smoke_pg.py" in workflow
