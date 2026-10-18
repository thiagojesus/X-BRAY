from collections.abc import Iterator, Sequence
from dataclasses import replace
from pathlib import Path

import pytest

from db.refresh_run_sql import Row, execute
from db.refresh_runs import (
    CycleId,
    RefreshLeaseConflictError,
    RefreshRunIdempotencyError,
    RefreshRunStart,
    RefreshWorkItemSpec,
    RunId,
    WorkItemKey,
    create_or_resume_run,
    read_run,
)

NOW = "2026-10-16T10:00:00+00:00"
LEASE_EXPIRY = "2026-10-16T10:30:00+00:00"
ITEMS = (
    RefreshWorkItemSpec(WorkItemKey("sgs-juros"), "sgs_juros", 0),
    RefreshWorkItemSpec(WorkItemKey("focus"), "focus", 1),
)


class _RecordingCursor:
    rowcount = 1

    def fetchone(self) -> Row | None:
        return None

    def fetchall(self) -> Sequence[Row]:
        return ()


class _RecordingConnection:
    def __init__(self) -> None:
        self.executions: list[tuple[str, Sequence[str | int | None]]] = []

    def execute(
        self,
        query: str,
        params: Sequence[str | int | None] = (),
    ) -> _RecordingCursor:
        self.executions.append((query, params))
        return _RecordingCursor()


@pytest.fixture
def isolated_database(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    from db import store

    if store._conn is not None:
        store._conn.close()
    store._conn = None
    monkeypatch.setattr(store, "DB_PATH", tmp_path / "refresh-runs.db")
    monkeypatch.setattr(store, "DATABASE_URL", "")
    store.init_db()
    yield
    if store._conn is not None:
        store._conn.close()
    store._conn = None


def _start(run_id: str, items: tuple[RefreshWorkItemSpec, ...] = ITEMS) -> RefreshRunStart:
    return RefreshRunStart(
        run_id=RunId(run_id),
        cycle_id=CycleId("cycle-original"),
        force=True,
        manifest_version="manifest-v7",
        derived_cutoff="2026-10-16T09:45:00+00:00",
        macro_cutoff="2026-10-16T09:50:00+00:00",
        items=items,
        now=NOW,
        lease_expires_at=LEASE_EXPIRY,
    )


def test_create_or_resume_preserves_run_cycle_and_cutoffs(isolated_database: None) -> None:
    # Given
    first = create_or_resume_run(_start("run-stable"))
    retry = _start("run-stable")

    # When
    resumed = create_or_resume_run(retry)

    # Then
    assert resumed.run.run_id == RunId("run-stable")
    assert resumed.run.cycle_id == "cycle-original"
    assert resumed.run.force is True
    assert resumed.run.manifest_version == "manifest-v7"
    assert resumed.run.derived_cutoff == "2026-10-16T09:45:00+00:00"
    assert resumed.run.macro_cutoff == "2026-10-16T09:50:00+00:00"
    assert resumed.run.created_at == first.run.created_at
    assert resumed.items == first.items


def test_start_rejects_a_conflicting_live_daily_refresh_lease(isolated_database: None) -> None:
    # Given
    create_or_resume_run(_start("run-owner"))

    # When
    with pytest.raises(RefreshLeaseConflictError) as conflict:
        create_or_resume_run(_start("run-contender"))

    # Then
    assert conflict.value.owner == RunId("run-owner")


def test_run_id_rejects_a_different_refresh_manifest(isolated_database: None) -> None:
    # Given
    original = create_or_resume_run(_start("run-idempotency"))
    changed = replace(_start("run-idempotency"), force=False)

    # When
    with pytest.raises(RefreshRunIdempotencyError):
        create_or_resume_run(changed)

    # Then
    stored = read_run(RunId("run-idempotency"))
    assert stored == original


def test_postgres_repository_adapter_rewrites_placeholders(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # Given
    from db import store

    connection = _RecordingConnection()
    monkeypatch.setattr(store, "uses_postgres", lambda: True)

    # When
    execute(connection, "UPDATE refresh_runs SET state = ? WHERE run_id = ?", ("running", "run-1"))

    # Then
    assert connection.executions == [
        ("UPDATE refresh_runs SET state = %s WHERE run_id = %s", ("running", "run-1"))
    ]
