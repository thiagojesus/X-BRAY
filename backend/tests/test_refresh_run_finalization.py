from collections.abc import Iterator
from pathlib import Path

import pytest

from db.refresh_runs import (
    CycleId,
    RefreshRunFinalization,
    RefreshRunFinalState,
    RefreshRunStart,
    RefreshRunState,
    RefreshWorkItemClaimRequest,
    RefreshWorkItemCompletion,
    RefreshWorkItemOutcome,
    RefreshWorkItemSpec,
    RunId,
    WorkItemKey,
    claim_next_work_item,
    complete_work_item_claim,
    create_or_resume_run,
    finalize_and_release_run,
    read_run,
)

NOW = "2026-10-16T10:00:00+00:00"
LEASE_EXPIRY = "2026-10-16T10:30:00+00:00"
ITEM = RefreshWorkItemSpec(WorkItemKey("focus"), "focus", 0)


@pytest.fixture
def isolated_database(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    from db import store

    if store._conn is not None:
        store._conn.close()
    store._conn = None
    monkeypatch.setattr(store, "DB_PATH", tmp_path / "refresh-finalization.db")
    monkeypatch.setattr(store, "DATABASE_URL", "")
    store.init_db()
    yield
    if store._conn is not None:
        store._conn.close()
    store._conn = None


def _start(run_id: str) -> RefreshRunStart:
    return RefreshRunStart(
        run_id=RunId(run_id),
        cycle_id=CycleId("cycle-final"),
        force=True,
        manifest_version="manifest-v7",
        derived_cutoff="2026-10-16T09:45:00+00:00",
        macro_cutoff="2026-10-16T09:50:00+00:00",
        items=(ITEM,),
        now=NOW,
        lease_expires_at=LEASE_EXPIRY,
    )


def _complete_successfully(run_id: str) -> None:
    create_or_resume_run(_start(run_id))
    claim = claim_next_work_item(
        RefreshWorkItemClaimRequest(RunId(run_id), NOW, LEASE_EXPIRY)
    )
    assert claim is not None
    assert claim.claim_token is not None
    assert complete_work_item_claim(
        RefreshWorkItemCompletion(
            run_id=RunId(run_id),
            item_key=ITEM.item_key,
            claim_token=claim.claim_token,
            status=RefreshWorkItemOutcome.SUCCEEDED,
            finished_at="2026-10-16T10:05:00+00:00",
            series_updated=1,
            record_count=12,
            error_code=None,
        )
    )


def test_successful_finalization_releases_lease(isolated_database: None) -> None:
    # Given
    from db import store

    _complete_successfully("run-finalize")
    finalization = RefreshRunFinalization(
        run_id=RunId("run-finalize"),
        state=RefreshRunFinalState.SUCCEEDED,
        final_result='{"status":"ok"}',
        finished_at="2026-10-16T10:06:00+00:00",
        last_successful_update="2026-10-16T10:05:00+00:00",
    )

    # When
    finalized = finalize_and_release_run(finalization)

    # Then
    assert finalized
    assert store._query("SELECT owner FROM refresh_leases WHERE lock_key = 'daily_refresh'") == []
    stored = read_run(RunId("run-finalize"))
    assert stored is not None
    assert stored.run.state is RefreshRunState.SUCCEEDED
    assert stored.run.final_result == '{"status":"ok"}'
    assert store.get_meta("last_successful_update") == "2026-10-16T10:05:00+00:00"


def test_terminal_result_is_immutable(isolated_database: None) -> None:
    # Given
    _complete_successfully("run-immutable")
    assert finalize_and_release_run(
        RefreshRunFinalization(
            run_id=RunId("run-immutable"),
            state=RefreshRunFinalState.SUCCEEDED,
            final_result='{"status":"ok"}',
            finished_at="2026-10-16T10:06:00+00:00",
        )
    )

    # When
    changed = finalize_and_release_run(
        RefreshRunFinalization(
            run_id=RunId("run-immutable"),
            state=RefreshRunFinalState.FAILED,
            final_result='{"status":"error"}',
            finished_at="2026-10-16T10:07:00+00:00",
        )
    )

    # Then
    assert not changed
    stored = read_run(RunId("run-immutable"))
    assert stored is not None
    assert stored.run.state is RefreshRunState.SUCCEEDED
    assert stored.run.final_result == '{"status":"ok"}'


def test_failed_work_cannot_finalize_as_success(isolated_database: None) -> None:
    # Given
    create_or_resume_run(_start("run-failure"))
    claim = claim_next_work_item(
        RefreshWorkItemClaimRequest(RunId("run-failure"), NOW, LEASE_EXPIRY)
    )
    assert claim is not None
    assert claim.claim_token is not None
    assert complete_work_item_claim(
        RefreshWorkItemCompletion(
            run_id=RunId("run-failure"),
            item_key=ITEM.item_key,
            claim_token=claim.claim_token,
            status=RefreshWorkItemOutcome.FAILED,
            finished_at="2026-10-16T10:05:00+00:00",
            series_updated=0,
            record_count=0,
            error_code="ProviderTimeout",
        )
    )

    # When
    finalized = finalize_and_release_run(
        RefreshRunFinalization(
            run_id=RunId("run-failure"),
            state=RefreshRunFinalState.SUCCEEDED,
            final_result='{"status":"ok"}',
            finished_at="2026-10-16T10:06:00+00:00",
        )
    )

    # Then
    assert not finalized
    stored = read_run(RunId("run-failure"))
    assert stored is not None
    assert stored.run.state is RefreshRunState.RUNNING
    assert stored.run.final_result is None


def test_retryable_failed_work_cannot_finalize_until_attempts_are_exhausted(
    isolated_database: None,
) -> None:
    # Given
    create_or_resume_run(_start("run-retry-finalize"))
    first_claim = claim_next_work_item(
        RefreshWorkItemClaimRequest(RunId("run-retry-finalize"), NOW, LEASE_EXPIRY)
    )
    assert first_claim is not None
    assert first_claim.claim_token is not None
    assert complete_work_item_claim(
        RefreshWorkItemCompletion(
            run_id=RunId("run-retry-finalize"),
            item_key=ITEM.item_key,
            claim_token=first_claim.claim_token,
            status=RefreshWorkItemOutcome.FAILED,
            finished_at="2026-10-16T10:05:00+00:00",
            series_updated=0,
            record_count=0,
            error_code="ProviderTimeout",
        )
    )

    # When
    premature = finalize_and_release_run(
        RefreshRunFinalization(
            run_id=RunId("run-retry-finalize"),
            state=RefreshRunFinalState.PARTIAL,
            final_result='{"status":"partial"}',
            finished_at="2026-10-16T10:05:01+00:00",
        )
    )

    # Then
    assert not premature
    retry_claim = claim_next_work_item(
        RefreshWorkItemClaimRequest(
            RunId("run-retry-finalize"),
            "2026-10-16T10:05:01+00:00",
            LEASE_EXPIRY,
        )
    )
    assert retry_claim is not None
    assert retry_claim.claim_token is not None
    assert retry_claim.attempt == 2
    assert complete_work_item_claim(
        RefreshWorkItemCompletion(
            run_id=RunId("run-retry-finalize"),
            item_key=ITEM.item_key,
            claim_token=retry_claim.claim_token,
            status=RefreshWorkItemOutcome.FAILED,
            finished_at="2026-10-16T10:06:00+00:00",
            series_updated=0,
            record_count=0,
            error_code="ProviderTimeout",
        )
    )
    assert finalize_and_release_run(
        RefreshRunFinalization(
            run_id=RunId("run-retry-finalize"),
            state=RefreshRunFinalState.PARTIAL,
            final_result='{"status":"partial"}',
            finished_at="2026-10-16T10:06:01+00:00",
        )
    )
