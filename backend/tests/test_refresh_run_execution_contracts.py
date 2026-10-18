from collections.abc import Iterator
from datetime import datetime
from pathlib import Path

import pytest

from db.refresh_run_models import CycleId, RunId
from refresh import RefreshDependencies
from refresh_run_execution import (
    RefreshRunAdvanceRequest,
    RefreshRunStartRequest,
    advance_refresh_run,
    start_refresh_run,
)
from tests.real_rate_support import isolated_database

pytestmark = pytest.mark.usefixtures(isolated_database.__name__)


class DerivedRetryError(Exception):
    pass


def test_retrying_derived_refresh_reuses_its_persisted_cutoff() -> None:
    # Given
    payload = {"data": [{"value": 1}]}
    derived_calls = 0
    derived_cutoffs: list[datetime] = []
    macro_cutoffs: list[datetime] = []
    cycle_ids: list[str] = []

    def derived(as_of: datetime, cycle_id: str) -> int:
        nonlocal derived_calls
        derived_calls += 1
        derived_cutoffs.append(as_of)
        cycle_ids.append(cycle_id)
        if derived_calls == 1:
            raise DerivedRetryError("temporary derived failure")
        return 6

    def macro(as_of: datetime, cycle_id: str) -> int:
        macro_cutoffs.append(as_of)
        cycle_ids.append(cycle_id)
        return 1

    dependencies = RefreshDependencies(
        fetch_sgs_batch=lambda *_args, **_kwargs: payload,
        fetch_all_focus=lambda **_kwargs: payload,
        fetch_focus_12m=lambda **_kwargs: payload,
        fetch_anbima_ima=lambda **_kwargs: payload,
        fetch_di_curves=lambda **_kwargs: payload,
        fetch_state_polls=lambda **_kwargs: payload,
        refresh_treasury_data=lambda: payload,
        refresh_sidra_data=lambda: payload,
        observe_inflation_target_rules=lambda: 11,
        refresh_derived_snapshots=derived,
        refresh_macro_score=macro,
    )
    started = start_refresh_run(
        RefreshRunStartRequest(RunId("run-cutoff-retry"), CycleId("cycle-cutoff-retry"), True),
        dependencies,
    )

    # When
    snapshot = started
    for item in started.items[:20]:
        snapshot = advance_refresh_run(
            RefreshRunAdvanceRequest(started.run.run_id, item.item_key),
            dependencies,
        )
    derived_item = started.items[20]
    failed_attempt = advance_refresh_run(
        RefreshRunAdvanceRequest(started.run.run_id, derived_item.item_key),
        dependencies,
    )
    first_cutoff = failed_attempt.run.derived_cutoff
    snapshot = advance_refresh_run(
        RefreshRunAdvanceRequest(started.run.run_id, derived_item.item_key),
        dependencies,
    )
    macro_item = started.items[21]
    snapshot = advance_refresh_run(
        RefreshRunAdvanceRequest(started.run.run_id, macro_item.item_key),
        dependencies,
    )

    # Then
    assert first_cutoff is not None
    assert derived_cutoffs[0] == derived_cutoffs[1]
    assert derived_cutoffs[1].isoformat() == first_cutoff
    assert macro_cutoffs == [derived_cutoffs[1]]
    assert cycle_ids == ["cycle-cutoff-retry"] * 3
    assert snapshot.run.derived_cutoff == snapshot.run.macro_cutoff == first_cutoff


@pytest.fixture
def isolated_database(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    from db import store

    if store._conn is not None:
        store._conn.close()
    store._conn = None
    monkeypatch.setattr(store, "DB_PATH", tmp_path / "refresh-run-execution-contracts.db")
    monkeypatch.setattr(store, "DATABASE_URL", "")
    store.init_db()
    yield
    if store._conn is not None:
        store._conn.close()
    store._conn = None
