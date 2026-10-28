import os
from collections.abc import Iterator
from dataclasses import replace
from datetime import datetime, timedelta, timezone
from pathlib import Path
from urllib.parse import urlsplit
from uuid import uuid4

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


@pytest.mark.parametrize(
    ("category", "expected_key"),
    [("invalid_schema", "focus"), ("network_reset", "sgs-juros")],
)
def test_v6_claim_skips_permanent_failed_item_but_retries_known_transport_failure(
    isolated_database: None, category: str, expected_key: str,
) -> None:
    from db import store

    start = replace(_start(f"typed-{category}"), manifest_version="daily-refresh-v6")
    create_or_resume_run(start)
    store._execute(
        "UPDATE refresh_work_items SET status = 'failed', attempt = 1, acquisition_attempts = 1, "
        "last_error_category = ?, last_error_code = 'typed-failure' "
        "WHERE run_id = ? AND item_key = 'sgs-juros'",
        (category, str(start.run_id)),
    )
    expected = WorkItemKey("sgs-juros" if expected_key == "sgs-juros" else "focus")
    claim = claim_next_work_item(RefreshWorkItemClaimRequest(
        start.run_id, NOW, LEASE_EXPIRY, expected,
    ))
    assert claim is not None
    assert claim.item_key == expected


def test_active_claim_blocks_claiming_another_work_item(isolated_database: None) -> None:
    # Given
    create_or_resume_run(_start("run-blocked"))
    first_claim = _claim("run-blocked")
    assert first_claim is not None

    # When
    second_claim = _claim("run-blocked", now="2026-10-16T10:01:00+00:00")

    # Then
    assert second_claim is None


def test_outbound_marker_is_idempotent_and_old_claim_cannot_defer_replacement(
    isolated_database: None,
) -> None:
    from db.refresh_attempt import begin_refresh_attempt, new_refresh_attempt
    from db.refresh_run_models import RefreshWorkItemDeferral
    from db.refresh_runs import defer_work_item_claim
    from db.store import _execute, _query, mark_refresh_acquisition_started

    create_or_resume_run(_start("run-marker-fence"))
    first = _claim("run-marker-fence")
    assert first is not None
    context = new_refresh_attempt(first)
    begin_refresh_attempt(context, NOW)
    mark_refresh_acquisition_started(context, NOW)
    mark_refresh_acquisition_started(context, NOW)
    assert _query(
        "SELECT acquisition_attempts FROM refresh_work_items WHERE run_id = ? AND item_key = ?",
        (str(first.run_id), str(first.item_key)),
    )[0]["acquisition_attempts"] == 1

    expired = "2026-10-16T10:01:00+00:00"
    _execute(
        "UPDATE refresh_work_items SET claim_expires_at = ? WHERE run_id = ? AND item_key = ?",
        (expired, str(first.run_id), str(first.item_key)),
    )
    replacement = claim_next_work_item(
        RefreshWorkItemClaimRequest(RunId("run-marker-fence"), "2026-10-16T10:02:00+00:00", LEASE_EXPIRY)
    )
    assert replacement is not None and replacement.claim_token != first.claim_token
    with pytest.raises(RuntimeError, match="no longer current"):
        mark_refresh_acquisition_started(context, "2026-10-16T10:02:00+00:00")
    result = defer_work_item_claim(RefreshWorkItemDeferral(
        first.run_id, first.item_key, first.claim_token,
        "2026-10-16T10:05:00+00:00", "retry_after", "2026-10-16T10:02:00+00:00",
    ))
    assert result is False
    current = _query(
        "SELECT status, claim_token, claim_generation, acquisition_attempts "
        "FROM refresh_work_items WHERE run_id = ? AND item_key = ?",
        (str(first.run_id), str(first.item_key)),
    )[0]
    assert current["status"] == "claimed"
    assert current["claim_token"] == replacement.claim_token
    assert current["claim_generation"] == 2
    assert current["acquisition_attempts"] == 1


def test_expired_new_attempt_reconciles_unknown_idempotently_without_invented_counts(
    isolated_database: None,
) -> None:
    from db.refresh_attempt import begin_refresh_attempt, new_refresh_attempt
    from db.store import _execute, _query, reconcile_expired_refresh_attempts

    create_or_resume_run(_start("run-reconcile-unknown"))
    claim = _claim("run-reconcile-unknown")
    assert claim is not None
    attempt = new_refresh_attempt(claim)
    begin_refresh_attempt(attempt, NOW)
    expiry = "2026-10-16T10:01:00+00:00"
    _execute(
        "UPDATE refresh_work_items SET claim_expires_at = ? WHERE run_id = ? AND item_key = ?",
        (expiry, str(claim.run_id), str(claim.item_key)),
    )
    _execute(
        "UPDATE refresh_attempt_lifecycle SET claim_expires_at = ? WHERE attempt_id = ?",
        (expiry, attempt.attempt_id),
    )

    observed = "2026-10-16T10:02:00+00:00"
    assert reconcile_expired_refresh_attempts(str(claim.run_id), observed) == 1
    assert reconcile_expired_refresh_attempts(str(claim.run_id), observed) == 0
    lifecycle = _query(
        "SELECT terminal_outcome, committed_count, source_data_date, last_error_code "
        "FROM refresh_attempt_lifecycle WHERE attempt_id = ?", (attempt.attempt_id,),
    )[0]
    reconciliation = _query(
        "SELECT outcome, committed_count, source_data_date, observed_at "
        "FROM refresh_attempt_reconciliations WHERE attempt_id = ?", (attempt.attempt_id,),
    )[0]
    assert lifecycle == {
        "terminal_outcome": "outcome_unknown",
        "committed_count": None,
        "source_data_date": None,
        "last_error_code": None,
    }
    assert reconciliation["outcome"] == "outcome_unknown"
    assert reconciliation["committed_count"] is None
    assert reconciliation["source_data_date"] is None
    assert reconciliation["observed_at"] == observed


def test_expired_sgs_attempt_uses_only_matching_atomic_unit_receipt(
    isolated_database: None,
) -> None:
    from db import store
    from db.refresh_attempt import (
        begin_refresh_attempt,
        install_refresh_attempt,
        new_refresh_attempt,
        reset_refresh_attempt,
    )
    from db.refresh_run_models import RefreshWorkItemSpec
    from db.sgs_refresh_plan import (
        SGSUnitCommitReceipt,
        build_sgs_plan,
        canonical_sgs_values,
        freeze_sgs_plan,
        plan_digest,
        plan_item_task_name,
    )

    claim_time = datetime.now(timezone.utc)
    claim_now = claim_time.isoformat()
    claim_expiry = (claim_time + timedelta(minutes=30)).isoformat()
    plan = build_sgs_plan(claim_time.date(), max_archive_units=0)
    unit = plan.units[0]
    task_name = plan_item_task_name(unit)
    start = replace(
        _start("run-reconcile-sgs", (RefreshWorkItemSpec(WorkItemKey(task_name), task_name, 0),)),
        manifest_version="daily-refresh-v6",
        now=claim_now,
        lease_expires_at=claim_expiry,
    )
    create_or_resume_run(start)
    freeze_sgs_plan("run-reconcile-sgs", plan)
    claim = _claim("run-reconcile-sgs", claim_now, claim_expiry)
    assert claim is not None
    attempt = new_refresh_attempt(claim)
    begin_refresh_attempt(attempt, claim_now)
    token = install_refresh_attempt(attempt)
    try:
        extraction_id = "sgs-commit-before-expiry"
        captured = claim_time
        store.start_source_extraction({
            "extraction_id": extraction_id, "source": "bcb_sgs", "indicator": str(unit.series_code),
            "qualifiers_json": "{}", "status": "running", "network_called": 1,
            "started_at": claim_now, "run_id": str(claim.run_id), "task_key": task_name,
            "attempt_id": attempt.attempt_id,
        })
        records = [{"data": unit.start_date.strftime("%d/%m/%Y"), "valor": "3.5"}]
        value_hash, count, reference_start, reference_end = canonical_sgs_values(unit.series_code, records)
        receipt = SGSUnitCommitReceipt(
            operation_id=extraction_id, extraction_id=extraction_id,
            run_id=str(claim.run_id), unit_id=unit.unit_id, plan_sha256=plan_digest(plan),
            series_code=unit.series_code, contract_id=unit.contract_id,
            contract_version=unit.contract_version, identity_kind=unit.identity_kind,
            scope_role=unit.role, policy_version=unit.policy_version,
            requested_start=unit.start_date, requested_end=unit.end_date,
            payload_sha256="e" * 64, canonical_value_sha256=value_hash,
            observed_count=count, reference_start=reference_start,
            reference_end=reference_end, captured_at=captured,
        )
        store.upsert_sgs(unit.series_code, records, sgs_unit_commit_receipt=receipt)
    finally:
        reset_refresh_attempt(token)
    expiry = (claim_time - timedelta(seconds=1)).isoformat()
    store._execute(
        "UPDATE refresh_work_items SET claim_expires_at = ? WHERE run_id = ? AND item_key = ?",
        (expiry, str(claim.run_id), str(claim.item_key)),
    )
    store._execute(
        "UPDATE refresh_attempt_lifecycle SET claim_expires_at = ? WHERE attempt_id = ?",
        (expiry, attempt.attempt_id),
    )
    assert store.reconcile_expired_refresh_attempts(
        str(claim.run_id), datetime.now(timezone.utc).isoformat()
    ) == 1
    row = store._query(
        "SELECT outcome, committed_count, source_data_date FROM refresh_attempt_reconciliations "
        "WHERE attempt_id = ?", (attempt.attempt_id,),
    )[0]
    assert row == {
        "outcome": "committed", "committed_count": count,
        "source_data_date": reference_end.isoformat(),
    }


def test_freshness_write_is_fenced_after_claim_rotation(isolated_database: None) -> None:
    from db.refresh_attempt import (
        install_refresh_attempt,
        new_refresh_attempt,
        reset_refresh_attempt,
    )
    from db.store import _execute, get_meta, set_meta

    create_or_resume_run(_start("run-fenced-freshness"))
    old_claim = _claim("run-fenced-freshness")
    assert old_claim is not None
    context = new_refresh_attempt(old_claim)
    token = install_refresh_attempt(context)
    try:
        _execute(
            "UPDATE refresh_work_items SET claim_token = ? WHERE run_id = ? AND item_key = ?",
            ("rotated-claim-token", str(old_claim.run_id), str(old_claim.item_key)),
        )
        with pytest.raises(RuntimeError, match="expired or has been replaced"):
            set_meta("sgs_last_refresh_432", "2026-10-16T10:01:00+00:00")
        assert get_meta("sgs_last_refresh_432") is None
    finally:
        reset_refresh_attempt(token)


def test_postgres_claim_fencing_uses_row_lock_and_keeps_token_private(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import psycopg

    from db import store
    from db.refresh_attempt import (
        begin_refresh_attempt,
        install_refresh_attempt,
        new_refresh_attempt,
        reset_refresh_attempt,
    )
    from db.store import set_meta

    url = os.environ.get("TEST_DATABASE_URL", "")
    target = urlsplit(url)
    if not url:
        pytest.skip("requires isolated local PostgreSQL TEST_DATABASE_URL")
    if target.hostname not in {"127.0.0.1", "localhost", "::1"} or not target.path.endswith("_test"):
        pytest.fail("TEST_DATABASE_URL must target a local *_test database")
    prior_url = store.DATABASE_URL
    store.DATABASE_URL = url
    store._pg_local.conn = None
    run_id = RunId(f"phasec-{uuid4()}")
    item_key = WorkItemKey("phasec-item")
    now = datetime.now(timezone.utc)
    now_text = now.isoformat()
    run_expiry = (now + timedelta(minutes=30)).isoformat()
    try:
        store.init_db()
        create_or_resume_run(RefreshRunStart(
            run_id=run_id,
            cycle_id=CycleId(f"cycle-{uuid4()}"),
            force=True,
            manifest_version="manifest-v7",
            derived_cutoff=None,
            macro_cutoff=None,
            items=(RefreshWorkItemSpec(item_key, "phasec_test", 0),),
            now=now_text,
            lease_expires_at=run_expiry,
        ))
        claim = claim_next_work_item(RefreshWorkItemClaimRequest(
            run_id, now_text, (now + timedelta(minutes=20)).isoformat()
        ))
        assert claim is not None
        context = new_refresh_attempt(claim)
        begin_refresh_attempt(context, now_text)
        token = install_refresh_attempt(context)
        try:
            set_meta("phasec_pg_fence_probe", now_text)
            with psycopg.connect(url) as connection:
                connection.execute(
                    "UPDATE refresh_work_items SET claim_token = %s WHERE run_id = %s AND item_key = %s",
                    ("rotated-secret-token", str(run_id), str(item_key)),
                )
            with pytest.raises(RuntimeError, match="expired or has been replaced"):
                set_meta("phasec_pg_fence_stale", now_text)
            assert store.get_meta("phasec_pg_fence_stale") is None
            assert store._query(
                "SELECT attempt_id FROM refresh_attempt_lifecycle WHERE attempt_id = ?",
                (context.attempt_id,),
            ) == [{"attempt_id": context.attempt_id}]
        finally:
            reset_refresh_attempt(token)
    finally:
        with psycopg.connect(url) as connection:
            connection.execute(
                "DELETE FROM refresh_attempt_milestones WHERE attempt_id IN "
                "(SELECT attempt_id FROM refresh_attempt_lifecycle WHERE run_id = %s)", (str(run_id),)
            )
            connection.execute(
                "DELETE FROM refresh_attempt_reconciliations WHERE attempt_id IN "
                "(SELECT attempt_id FROM refresh_attempt_lifecycle WHERE run_id = %s)", (str(run_id),)
            )
            connection.execute("DELETE FROM refresh_attempt_lifecycle WHERE run_id = %s", (str(run_id),))
            connection.execute("DELETE FROM refresh_work_items WHERE run_id = %s", (str(run_id),))
            connection.execute("DELETE FROM refresh_leases WHERE owner = %s", (str(run_id),))
            connection.execute("DELETE FROM refresh_runs WHERE run_id = %s", (str(run_id),))
            connection.execute("DELETE FROM meta WHERE key LIKE 'phasec_pg_fence_%'")
        store._pg_local.conn = None
        store.DATABASE_URL = prior_url


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
