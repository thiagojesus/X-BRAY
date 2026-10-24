from collections.abc import Iterator
from dataclasses import replace
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from config import IPCA_GROUPS
from datafetchers.bcb_sgs import InvalidSGSPayloadError
from db.store import get_meta, set_meta, upsert_sgs
from db.refresh_run_models import (
    CycleId,
    RefreshRunStart,
    RefreshRunState,
    RefreshWorkItemStatus,
    RunId,
)
from db.refresh_runs import create_or_resume_run
from refresh import RefreshDependencies
from refresh_run_tasks import _run_legacy_ipca_group
from refresh_run_execution import (
    RefreshRunAdvanceRequest,
    RefreshRunStartRequest,
    _manifest,
    advance_refresh_run,
    get_refresh_run,
    start_refresh_run,
)
from tests.real_rate_support import isolated_database

pytestmark = pytest.mark.usefixtures(isolated_database.__name__)


def _dependencies() -> RefreshDependencies:
    payload = {"data": [{"value": 1}]}
    return RefreshDependencies(
        fetch_sgs_batch=lambda *_args, **_kwargs: payload,
        fetch_all_focus=lambda **_kwargs: payload,
        fetch_focus_12m=lambda **_kwargs: payload,
        fetch_anbima_ima=lambda **_kwargs: payload,
        fetch_di_curves=lambda **_kwargs: payload,
        fetch_state_polls=lambda **_kwargs: payload,
        refresh_treasury_data=lambda: payload,
        refresh_sidra_data=lambda: payload,
        refresh_derived_snapshots=lambda _as_of, _cycle_id: 3,
        observe_inflation_target_rules=lambda: 11,
        refresh_macro_score=lambda _as_of, _cycle_id: 1,
    )


def test_repeating_a_completed_item_does_not_execute_the_next_source_task() -> None:
    # Given
    calls = 0
    payload = {"data": [{"value": 1}]}

    def fetch(*_args, **_kwargs):
        nonlocal calls
        calls += 1
        return payload

    dependencies = RefreshDependencies(
        fetch_sgs_batch=fetch,
        fetch_all_focus=fetch,
        fetch_focus_12m=fetch,
        fetch_anbima_ima=fetch,
        fetch_di_curves=fetch,
        fetch_state_polls=fetch,
        refresh_treasury_data=fetch,
        refresh_sidra_data=fetch,
        refresh_derived_snapshots=lambda _as_of, _cycle_id: 0,
    )
    request = RefreshRunStartRequest(RunId("run-one-source"), CycleId("cycle-one-source"), True)
    started = start_refresh_run(request, dependencies)
    first_item = started.items[0]
    advance = RefreshRunAdvanceRequest(started.run.run_id, first_item.item_key)

    # When
    first_result = advance_refresh_run(advance, dependencies)
    replayed_result = advance_refresh_run(advance, dependencies)

    # Then
    assert calls == 1
    assert first_result.items[0].status is RefreshWorkItemStatus.SUCCEEDED
    assert replayed_result.items[0].status is RefreshWorkItemStatus.SUCCEEDED
    assert replayed_result.items[1].status is RefreshWorkItemStatus.PENDING


def test_v2_refresh_manifest_splits_large_ipca_group_task_into_single_windows() -> None:
    # Given
    started = start_refresh_run(
        RefreshRunStartRequest(RunId("windowed-groups"), CycleId("windowed-groups"), True),
        _dependencies(),
    )

    # When
    group_items = [item for item in started.items if item.task.startswith("sgs_ipca_grupos_")]

    # Then
    assert started.run.manifest_version == "daily-refresh-v2"
    assert len(group_items) == 36
    assert len({item.task for item in group_items}) == 36
    assert all(item.status is RefreshWorkItemStatus.PENDING for item in group_items)


def test_invalid_ipca_window_fails_run_without_overwriting_cached_data_or_freshness() -> None:
    # Given
    from db import store

    series_code = next(iter(IPCA_GROUPS.values()))
    previous_freshness = "2026-10-01T00:00:00+00:00"
    cached_record = {"data": "01/01/2025", "valor": "0.00"}
    upsert_sgs(series_code, [cached_record])
    set_meta(f"sgs_last_refresh_{series_code}", previous_freshness)
    invalid_payload = InvalidSGSPayloadError(
        series_code,
        "01/01/1990",
        "01/01/2000",
        "expected-list",
    )

    def fetch_sgs_batch(codes, **_kwargs):
        if series_code in codes.values():
            raise invalid_payload
        return {name: [cached_record] for name in codes}

    dependencies = replace(_dependencies(), fetch_sgs_batch=fetch_sgs_batch)
    started = start_refresh_run(
        RefreshRunStartRequest(RunId("invalid-ipca-window"), CycleId("invalid-ipca-window"), True),
        dependencies,
    )
    assert started.items[8].task == f"sgs_ipca_grupos_{series_code}_0"
    for item in started.items[:8]:
        advance_refresh_run(RefreshRunAdvanceRequest(started.run.run_id, item.item_key), dependencies)

    # When
    failed = advance_refresh_run(
        RefreshRunAdvanceRequest(started.run.run_id, started.items[8].item_key),
        dependencies,
    )

    # Then
    failed_item = failed.items[8]
    assert failed_item.status is RefreshWorkItemStatus.FAILED
    assert failed_item.error_code == invalid_payload.error_code
    assert get_meta(f"sgs_last_refresh_{series_code}") == previous_freshness
    assert store.query_sgs(series_code) == [{"data": "01/01/2025", "valor": "0.0"}]


def test_expired_legacy_claim_resumes_same_run_and_exact_manifest() -> None:
    # Given
    from db import store

    dependencies = replace(
        _dependencies(),
        fetch_sgs_batch=lambda codes, **_kwargs: {
            **{"data": [{"value": 1}]},
            **{name: [{"value": 1}] for name in codes},
        },
    )
    run_id = RunId("legacy-expired-run")
    cycle_id = CycleId("legacy-expired-run")
    now = datetime.now(timezone.utc)
    legacy_items = _manifest(False, dependencies, "daily-refresh-v1")
    created = create_or_resume_run(
        RefreshRunStart(
            run_id=run_id,
            cycle_id=cycle_id,
            force=False,
            manifest_version="daily-refresh-v1",
            derived_cutoff=None,
            macro_cutoff=None,
            items=legacy_items,
            now=now.isoformat(),
            lease_expires_at=(now + timedelta(minutes=30)).isoformat(),
        )
    )
    for previous_item in created.items[:8]:
        advance_refresh_run(
            RefreshRunAdvanceRequest(run_id, previous_item.item_key),
            dependencies,
        )
    group_item = created.items[8]
    assert group_item.task == "sgs_ipca_grupos"
    expired_at = (now - timedelta(seconds=1)).isoformat()
    store._execute(
        "UPDATE refresh_leases SET expires_at = ? WHERE lock_key = 'daily_refresh'",
        (expired_at,),
    )
    store._execute(
        "UPDATE refresh_work_items SET status='claimed', attempt=1, claim_token='expired-token', "
        "claim_expires_at=?, updated_at=? WHERE run_id=? AND item_key=?",
        (expired_at, expired_at, run_id, group_item.item_key),
    )

    # When
    resumed = start_refresh_run(
        RefreshRunStartRequest(run_id, cycle_id, False),
        dependencies,
    )
    advanced = advance_refresh_run(
        RefreshRunAdvanceRequest(run_id, group_item.item_key),
        dependencies,
    )

    # Then
    assert resumed.run.manifest_version == "daily-refresh-v1"
    assert [(item.item_key, item.task, item.task_order) for item in resumed.items] == [
        (item.item_key, item.task, item.task_order) for item in created.items
    ]
    assert advanced.items[8].attempt == 2
    assert advanced.items[8].task == "sgs_ipca_grupos"
    assert advanced.items[8].status is RefreshWorkItemStatus.SUCCEEDED
    assert all(item.status is RefreshWorkItemStatus.SUCCEEDED for item in advanced.items[:9])


def test_legacy_ipca_manifest_uses_bounded_requests_and_marks_fresh_only_at_end(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: list[dict[str, object]] = []
    metadata: list[tuple[str, str]] = []

    def fetch(codes: dict[str, int], **kwargs: object) -> dict[str, object]:
        calls.append(kwargs)
        name = next(iter(codes))
        return {name: [{"value": len(calls)}]}

    monkeypatch.setattr("refresh_run_tasks.set_meta", lambda key, value: metadata.append((key, value)))

    result = _run_legacy_ipca_group(RefreshDependencies(fetch_sgs_batch=fetch), False)

    assert len(calls) == 36
    assert all(call["request_timeout"] <= 5 for call in calls)
    assert all(call["fail_fast"] is True and call["mark_refreshed"] is False for call in calls)
    assert len(result) == 9
    assert len(metadata) == 10


def test_legacy_ipca_manifest_does_not_mark_fresh_after_partial_failure(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls = 0
    metadata: list[tuple[str, str]] = []

    def fetch(_codes: dict[str, int], **_kwargs: object) -> dict[str, object]:
        nonlocal calls
        calls += 1
        if calls == 2:
            raise TimeoutError("provider timeout")
        return {"alimentacao_bebidas": [{"value": 1}]}

    monkeypatch.setattr("refresh_run_tasks.set_meta", lambda key, value: metadata.append((key, value)))

    with pytest.raises(TimeoutError, match="provider timeout"):
        _run_legacy_ipca_group(RefreshDependencies(fetch_sgs_batch=fetch), False)

    assert calls == 2
    assert metadata == []


def test_resumable_phases_run_after_sources_with_the_same_cutoff_and_cycle() -> None:
    # Given
    source_calls = 0
    events: list[str] = []
    derived_identity: tuple[datetime, str] | None = None
    macro_identity: tuple[datetime, str] | None = None
    payload = {"data": [{"value": 1}]}

    def source(*_args, **_kwargs):
        nonlocal source_calls
        source_calls += 1
        return payload

    def observe() -> int:
        assert source_calls == 54
        events.append("observe")
        return 11

    def derive(as_of: datetime, cycle_id: str) -> int:
        nonlocal derived_identity
        derived_identity = (as_of, cycle_id)
        events.append("derive")
        return 6

    def macro(as_of: datetime, cycle_id: str) -> int:
        nonlocal macro_identity
        macro_identity = (as_of, cycle_id)
        events.append("macro")
        return 1

    dependencies = RefreshDependencies(
        fetch_sgs_batch=source,
        fetch_all_focus=source,
        fetch_focus_12m=source,
        fetch_anbima_ima=source,
        fetch_di_curves=source,
        fetch_state_polls=source,
        refresh_treasury_data=source,
        refresh_sidra_data=source,
        refresh_derived_snapshots=derive,
        observe_inflation_target_rules=observe,
        refresh_macro_score=macro,
    )
    started = start_refresh_run(
        RefreshRunStartRequest(RunId("run-phases"), CycleId("cycle-phases"), True),
        dependencies,
    )

    # When
    snapshot = started
    for item in started.items:
        snapshot = advance_refresh_run(
            RefreshRunAdvanceRequest(started.run.run_id, item.item_key),
            dependencies,
        )

    # Then
    assert source_calls == 54
    assert events == ["observe", "derive", "macro"]
    assert derived_identity is not None
    assert macro_identity == derived_identity
    assert snapshot.run.state is RefreshRunState.SUCCEEDED
    assert snapshot.run.derived_cutoff == snapshot.run.macro_cutoff
    assert all(item.status is RefreshWorkItemStatus.SUCCEEDED for item in snapshot.items)
    assert get_refresh_run(started.run.run_id) == snapshot


def test_all_failed_sources_skip_derived_phases_without_advancing_success_marker() -> None:
    # Given
    phase_calls: list[str] = []
    payload = {"error": "provider unavailable"}
    dependencies = RefreshDependencies(
        fetch_sgs_batch=lambda *_args, **_kwargs: payload,
        fetch_all_focus=lambda **_kwargs: payload,
        fetch_focus_12m=lambda **_kwargs: payload,
        fetch_anbima_ima=lambda **_kwargs: payload,
        fetch_di_curves=lambda **_kwargs: payload,
        fetch_state_polls=lambda **_kwargs: payload,
        refresh_treasury_data=lambda: payload,
        refresh_sidra_data=lambda: payload,
        refresh_derived_snapshots=lambda _as_of, _cycle_id: phase_calls.append("derived") or 0,
        observe_inflation_target_rules=lambda: phase_calls.append("observe") or 11,
        refresh_macro_score=lambda _as_of, _cycle_id: phase_calls.append("macro") or 1,
    )
    started = start_refresh_run(
        RefreshRunStartRequest(RunId("run-all-source-failure"), CycleId("cycle-failure"), True),
        dependencies,
    )

    # When
    snapshot = started
    for item in started.items[:54]:
        for _attempt in range(2):
            snapshot = advance_refresh_run(
                RefreshRunAdvanceRequest(started.run.run_id, item.item_key),
                dependencies,
            )
    for item in started.items[54:]:
        snapshot = advance_refresh_run(
            RefreshRunAdvanceRequest(started.run.run_id, item.item_key),
            dependencies,
        )

    # Then
    from db import store

    assert phase_calls == []
    assert snapshot.run.state is RefreshRunState.FAILED
    assert all(item.status is RefreshWorkItemStatus.FAILED for item in snapshot.items[:54])
    assert all(item.status is RefreshWorkItemStatus.SKIPPED for item in snapshot.items[54:])
    assert store.get_meta("last_successful_update") is None


@pytest.fixture
def isolated_database(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    from db import store

    if store._conn is not None:
        store._conn.close()
    store._conn = None
    monkeypatch.setattr(store, "DB_PATH", tmp_path / "refresh-run-execution.db")
    monkeypatch.setattr(store, "DATABASE_URL", "")
    store.init_db()
    yield
    if store._conn is not None:
        store._conn.close()
    store._conn = None
