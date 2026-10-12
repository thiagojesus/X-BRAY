from datetime import datetime, timezone
from pathlib import Path

import pytest

from derived.models import (
    DerivedInput,
    DerivedResult,
    FutureDerivedInputError,
    SnapshotOrigin,
    UnavailableReason,
)
from derived.repository import (
    insert_snapshot,
    query_latest_snapshots,
    query_snapshot_history,
)


@pytest.fixture
def isolated_database(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    from db import store

    if store._conn is not None:
        store._conn.close()
    store._conn = None
    monkeypatch.setattr(store, "DB_PATH", tmp_path / "derived.db")
    monkeypatch.setattr(store, "DATABASE_URL", "")
    store.init_db()
    yield
    if store._conn is not None:
        store._conn.close()
    store._conn = None


def _result(value: float, input_value: float) -> DerivedResult:
    as_of = datetime(2026, 9, 27, 12, tzinfo=timezone.utc)
    return DerivedResult(
        key="sample",
        value=value,
        unit="ratio",
        as_of=as_of,
        methodology_version="1.0.0",
        formula="series_a / 2",
        inputs=(DerivedInput(
            key="series_a",
            value=input_value,
            unit="pct",
            reference_period="2026-08-01",
            available_at=as_of,
            source="fixture",
        ),),
        reason=None,
    )


def test_snapshot_insert_is_idempotent_and_deterministic(isolated_database: None) -> None:
    result = _result(5.0, 10.0)

    first = insert_snapshot(result, calculation_cycle_id="cycle-a", origin=SnapshotOrigin.TEST_FIXTURE)
    second = insert_snapshot(result, calculation_cycle_id="cycle-b", origin=SnapshotOrigin.TEST_FIXTURE)
    history = query_snapshot_history("sample")

    assert first is True
    assert second is False
    assert len(history) == 1
    assert history[0].result == result


def test_revised_input_appends_snapshot_without_rewriting_prior_payload(isolated_database: None) -> None:
    first_result = _result(5.0, 10.0)
    revised_result = _result(6.0, 12.0)

    insert_snapshot(first_result, calculation_cycle_id="cycle-a", origin=SnapshotOrigin.TEST_FIXTURE)
    insert_snapshot(revised_result, calculation_cycle_id="cycle-b", origin=SnapshotOrigin.TEST_FIXTURE)
    history = query_snapshot_history("sample")

    assert [snapshot.result.value for snapshot in history] == [5.0, 6.0]
    assert len({snapshot.inputs_hash for snapshot in history}) == 2
    assert query_latest_snapshots()["sample"].result == revised_result


def test_future_input_result_cannot_be_persisted(isolated_database: None) -> None:
    as_of = datetime(2026, 9, 27, 12, tzinfo=timezone.utc)

    with pytest.raises(FutureDerivedInputError, match="available at as_of"):
        insert_snapshot(
            DerivedResult(
                key="sample",
                value=None,
                unit="ratio",
                as_of=as_of,
                methodology_version="1.0.0",
                formula="future_series",
                inputs=(DerivedInput(
                    key="future_series",
                    value=987654321.25,
                    unit="future_unit",
                    reference_period="2099-12-31",
                    available_at=datetime(2026, 9, 27, 13, tzinfo=timezone.utc),
                    source="future_source",
                ),),
                reason=UnavailableReason.INPUT_NOT_AVAILABLE,
            ),
            calculation_cycle_id="cycle-future",
            origin=SnapshotOrigin.TEST_FIXTURE,
        )

    assert query_snapshot_history("sample") == []


def test_migration_is_repeatable_and_records_version(isolated_database: None) -> None:
    from db import store

    store.init_db()

    assert store._query(
        "SELECT version FROM schema_migrations WHERE version = '003_derived_snapshots'"
    ) == [{"version": "003_derived_snapshots"}]
