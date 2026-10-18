from collections.abc import Iterator
from datetime import datetime
from pathlib import Path

import pytest

from db.refresh_run_models import (
    CycleId,
    RefreshRunState,
    RefreshWorkItemStatus,
    RunId,
)
from refresh import RefreshDependencies
from refresh_run_execution import (
    RefreshRunAdvanceRequest,
    RefreshRunStartRequest,
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
        assert source_calls == 19
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
    assert source_calls == 19
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
    for item in started.items[:19]:
        for _attempt in range(2):
            snapshot = advance_refresh_run(
                RefreshRunAdvanceRequest(started.run.run_id, item.item_key),
                dependencies,
            )
    for item in started.items[19:]:
        snapshot = advance_refresh_run(
            RefreshRunAdvanceRequest(started.run.run_id, item.item_key),
            dependencies,
        )

    # Then
    from db import store

    assert phase_calls == []
    assert snapshot.run.state is RefreshRunState.FAILED
    assert all(item.status is RefreshWorkItemStatus.FAILED for item in snapshot.items[:19])
    assert all(item.status is RefreshWorkItemStatus.SKIPPED for item in snapshot.items[19:])
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
