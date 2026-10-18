from collections.abc import Iterator
from dataclasses import replace
from pathlib import Path

import pytest

from db.refresh_runs import (
    ClaimToken,
    CycleId,
    RefreshLeaseConflictError,
    RefreshRunStart,
    RefreshWorkItemClaimRequest,
    RefreshWorkItemCompletion,
    RefreshWorkItemOutcome,
    RefreshWorkItemRecord,
    RefreshWorkItemSpec,
    RefreshWorkItemStatus,
    RunId,
    WorkItemKey,
    claim_next_work_item,
    complete_work_item_claim,
    create_or_resume_run,
    read_run,
)

NOW = "2026-10-16T10:00:00+00:00"
LEASE_EXPIRY = "2026-10-16T10:30:00+00:00"
ITEMS = (
    RefreshWorkItemSpec(WorkItemKey("sgs-juros"), "sgs_juros", 0),
    RefreshWorkItemSpec(WorkItemKey("focus"), "focus", 1),
)


@pytest.fixture
def isolated_database(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    from db import store

    if store._conn is not None:
        store._conn.close()
    store._conn = None
    monkeypatch.setattr(store, "DB_PATH", tmp_path / "refresh-claims.db")
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


def _claim(
    run_id: str,
    now: str = NOW,
    expires_at: str = LEASE_EXPIRY,
) -> RefreshWorkItemRecord | None:
    return claim_next_work_item(RefreshWorkItemClaimRequest(RunId(run_id), now, expires_at))


def _completion(run_id: str, item_key: WorkItemKey, token: ClaimToken) -> RefreshWorkItemCompletion:
    return RefreshWorkItemCompletion(
        run_id=RunId(run_id),
        item_key=item_key,
        claim_token=token,
        status=RefreshWorkItemOutcome.SUCCEEDED,
        finished_at="2026-10-16T10:05:00+00:00",
        series_updated=1,
        record_count=12,
        error_code=None,
    )


def test_active_claim_blocks_claiming_another_work_item(isolated_database: None) -> None:
    # Given
    create_or_resume_run(_start("run-blocked"))
    first_claim = _claim("run-blocked")
    assert first_claim is not None

    # When
    second_claim = _claim("run-blocked", now="2026-10-16T10:01:00+00:00")

    # Then
    assert second_claim is None


def test_specific_claim_cannot_skip_an_earlier_pending_item(isolated_database: None) -> None:
    # Given
    create_or_resume_run(_start("run-ordered-claim"))

    # When
    claim = claim_next_work_item(
        RefreshWorkItemClaimRequest(
            RunId("run-ordered-claim"),
            NOW,
            LEASE_EXPIRY,
            WorkItemKey("focus"),
        )
    )

    # Then
    assert claim is None


def test_claim_completion_requires_the_current_attempt_token(isolated_database: None) -> None:
    # Given
    create_or_resume_run(_start("run-fenced"))
    claim = _claim("run-fenced")
    assert claim is not None
    assert claim.claim_token is not None

    # When
    stale = _completion("run-fenced", claim.item_key, ClaimToken("stale-token"))
    current = _completion("run-fenced", claim.item_key, claim.claim_token)

    # Then
    assert not complete_work_item_claim(stale)
    assert complete_work_item_claim(current)
    assert not complete_work_item_claim(current)
    stored = read_run(RunId("run-fenced"))
    assert stored is not None
    assert stored.items[0].claim_token == claim.claim_token
    assert stored.items[0].claim_expires_at == LEASE_EXPIRY
    assert stored.items[0].series_updated == 1
    assert stored.items[0].record_count == 12


def test_expired_claim_is_recovered_with_a_new_fencing_token(isolated_database: None) -> None:
    # Given
    create_or_resume_run(_start("run-recover"))
    original = _claim("run-recover", expires_at="2026-10-16T10:01:00+00:00")
    assert original is not None
    assert original.claim_token is not None

    # When
    recovered = _claim(
        "run-recover",
        now="2026-10-16T10:01:00+00:00",
        expires_at="2026-10-16T10:31:00+00:00",
    )

    # Then
    assert recovered is not None
    assert recovered.claim_token is not None
    assert recovered.attempt == 2
    assert recovered.claim_token != original.claim_token
    assert not complete_work_item_claim(
        _completion("run-recover", original.item_key, original.claim_token)
    )
    assert complete_work_item_claim(
        _completion("run-recover", recovered.item_key, recovered.claim_token)
    )


def test_expired_run_is_terminalized_when_another_run_takes_the_lease(isolated_database: None) -> None:
    # Given
    expired_run = replace(
        _start("run-expired", ITEMS[:1]),
        lease_expires_at="2026-10-16T10:30:00+00:00",
    )
    create_or_resume_run(expired_run)
    claim = _claim("run-expired", expires_at="2026-10-16T10:06:00+00:00")
    assert claim is not None
    new_run = replace(
        _start("run-takeover", ITEMS[:1]),
        now="2026-10-16T10:30:00+00:00",
        lease_expires_at="2026-10-16T11:00:00+00:00",
    )

    # When
    create_or_resume_run(new_run)

    # Then
    expired = read_run(RunId("run-expired"))
    assert expired is not None
    assert expired.run.state.value == "failed"
    assert expired.items[0].status is RefreshWorkItemStatus.FAILED
    assert expired.items[0].error_code == "RefreshLeaseExpired"


def test_expired_claim_stops_retrying_after_two_attempts(isolated_database: None) -> None:
    # Given
    create_or_resume_run(_start("run-attempt-limit", ITEMS[:1]))
    first = _claim("run-attempt-limit", expires_at="2026-10-16T10:01:00+00:00")
    assert first is not None
    assert first.claim_token is not None
    assert complete_work_item_claim(
        replace(
            _completion("run-attempt-limit", first.item_key, first.claim_token),
            status=RefreshWorkItemOutcome.FAILED,
            finished_at="2026-10-16T10:00:30+00:00",
            series_updated=0,
            record_count=0,
            error_code="ProviderTimeout",
        )
    )
    second = _claim(
        "run-attempt-limit",
        now="2026-10-16T10:00:30+00:00",
        expires_at="2026-10-16T10:01:30+00:00",
    )
    assert second is not None

    # When
    third = _claim(
        "run-attempt-limit",
        now="2026-10-16T10:01:30+00:00",
        expires_at="2026-10-16T10:07:30+00:00",
    )

    # Then
    assert third is None
    stored = read_run(RunId("run-attempt-limit"))
    assert stored is not None
    assert stored.items[0].status is RefreshWorkItemStatus.FAILED
    assert stored.items[0].attempt == 2
    assert stored.items[0].claim_token is None
    assert stored.items[0].error_code == "AttemptLimitExceeded"


def test_expired_run_lease_fences_its_still_live_claim(isolated_database: None) -> None:
    # Given
    expired_run = replace(
        _start("run-expired", ITEMS[:1]),
        lease_expires_at="2026-10-16T10:01:00+00:00",
    )
    create_or_resume_run(expired_run)
    old_claim = _claim("run-expired", expires_at="2026-10-16T10:10:00+00:00")
    assert old_claim is not None
    assert old_claim.claim_token is not None
    early_run = replace(
        _start("run-takeover", ITEMS[:1]),
        now="2026-10-16T10:01:00+00:00",
        lease_expires_at="2026-10-16T10:31:00+00:00",
    )

    # When
    with pytest.raises(RefreshLeaseConflictError):
        create_or_resume_run(early_run)

    next_run = replace(
        early_run,
        now="2026-10-16T10:10:00+00:00",
        lease_expires_at="2026-10-16T10:40:00+00:00",
    )
    create_or_resume_run(next_run)
    stale = _completion("run-expired", old_claim.item_key, old_claim.claim_token)

    # Then
    assert not complete_work_item_claim(stale)
    old_state = read_run(RunId("run-expired"))
    assert old_state is not None
    assert old_state.items[0].status is RefreshWorkItemStatus.FAILED
    assert old_state.items[0].series_updated == 0
