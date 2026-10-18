from collections.abc import Iterator
from pathlib import Path

import pytest

from db.refresh_run_models import (
    CycleId,
    RefreshRunCutoffOrderError,
    RefreshRunCutoffPhase,
    RefreshRunCutoffUpdate,
    RefreshRunStart,
    RefreshWorkItemSpec,
    RunId,
    WorkItemKey,
)
from db.refresh_runs import create_or_resume_run, persist_run_cutoff, read_run

NOW = "2026-10-16T10:00:00+00:00"
LEASE_EXPIRY = "2026-10-16T10:30:00+00:00"
ITEMS = (RefreshWorkItemSpec(WorkItemKey("focus"), "focus", 0),)


@pytest.fixture
def isolated_database(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    from db import store

    if store._conn is not None:
        store._conn.close()
    store._conn = None
    monkeypatch.setattr(store, "DB_PATH", tmp_path / "refresh-run-cutoffs.db")
    monkeypatch.setattr(store, "DATABASE_URL", "")
    store.init_db()
    yield
    if store._conn is not None:
        store._conn.close()
    store._conn = None


def _start() -> RefreshRunStart:
    return RefreshRunStart(
        run_id=RunId("cutoff-run"),
        cycle_id=CycleId("cutoff-cycle"),
        force=False,
        manifest_version="manifest-v1",
        derived_cutoff=None,
        macro_cutoff=None,
        items=ITEMS,
        now=NOW,
        lease_expires_at=LEASE_EXPIRY,
    )


def _persist(phase: RefreshRunCutoffPhase, cutoff: str, now: str) -> str:
    return persist_run_cutoff(
        RefreshRunCutoffUpdate(
            run_id=RunId("cutoff-run"),
            phase=phase,
            cutoff=cutoff,
            now=now,
            lease_expires_at="2026-10-16T10:45:00+00:00",
        )
    )


def test_derived_and_macro_cutoffs_are_persisted_once(isolated_database: None) -> None:
    # Given
    create_or_resume_run(_start())

    # When
    derived_cutoff = _persist(
        RefreshRunCutoffPhase.DERIVED,
        "2026-10-16T10:01:00+00:00",
        "2026-10-16T10:01:00+00:00",
    )
    macro_cutoff = _persist(
        RefreshRunCutoffPhase.MACRO_SCORE,
        "2026-10-16T10:01:00+00:00",
        "2026-10-16T10:02:00+00:00",
    )
    retried_derived = _persist(
        RefreshRunCutoffPhase.DERIVED,
        "2026-10-16T10:03:00+00:00",
        "2026-10-16T10:03:00+00:00",
    )

    # Then
    stored = read_run(RunId("cutoff-run"))
    assert derived_cutoff == "2026-10-16T10:01:00+00:00"
    assert macro_cutoff == derived_cutoff
    assert retried_derived == derived_cutoff
    assert stored is not None
    assert stored.run.derived_cutoff == derived_cutoff
    assert stored.run.macro_cutoff == macro_cutoff


def test_macro_cutoff_requires_a_persisted_derived_cutoff(isolated_database: None) -> None:
    # Given
    create_or_resume_run(_start())

    # When / Then
    with pytest.raises(RefreshRunCutoffOrderError):
        _persist(
            RefreshRunCutoffPhase.MACRO_SCORE,
            "2026-10-16T10:02:00+00:00",
            "2026-10-16T10:02:00+00:00",
        )


def test_macro_score_can_reuse_the_derived_no_lookahead_cutoff(isolated_database: None) -> None:
    # Given
    create_or_resume_run(_start())
    shared_cutoff = "2026-10-16T10:01:00+00:00"
    _persist(RefreshRunCutoffPhase.DERIVED, shared_cutoff, shared_cutoff)

    # When
    macro_cutoff = _persist(
        RefreshRunCutoffPhase.MACRO_SCORE,
        shared_cutoff,
        "2026-10-16T10:02:00+00:00",
    )

    # Then
    assert macro_cutoff == shared_cutoff


def test_start_resumes_after_cutoffs_were_persisted(isolated_database: None) -> None:
    # Given
    original = create_or_resume_run(_start())
    _persist(
        RefreshRunCutoffPhase.DERIVED,
        "2026-10-16T10:01:00+00:00",
        "2026-10-16T10:01:00+00:00",
    )

    # When
    resumed = create_or_resume_run(_start())

    # Then
    assert resumed.run.run_id == original.run.run_id
    assert resumed.run.derived_cutoff == "2026-10-16T10:01:00+00:00"
