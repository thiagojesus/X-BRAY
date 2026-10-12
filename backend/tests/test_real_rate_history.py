from datetime import date, datetime, timedelta, timezone

import pytest

from derived.models import SnapshotOrigin
from derived.repository import (
    insert_snapshot,
    query_snapshot_history,
    refresh_derived_snapshots,
)
from tests.real_rate_support import (
    AS_OF,
    REAL_RATE_KEY,
    _history_result,
    _refresh_result,
    _seed_di,
    _seed_focus,
    isolated_database,
)


def test_history_change_rekeys_same_cutoff_without_future_or_stale_noise() -> None:
    # Given
    _seed_focus()
    _seed_di()
    assert refresh_derived_snapshots(AS_OF, "current-first") == 1
    eligible = _history_result(
        reference_date=date(2026, 8, 1),
        value=1.0,
        as_of=datetime(2026, 8, 2, 12, tzinfo=timezone.utc),
    )
    assert insert_snapshot(
        eligible,
        calculation_cycle_id="eligible-history",
        origin=SnapshotOrigin.TEST_FIXTURE,
    )

    # When
    second_inserted = refresh_derived_snapshots(AS_OF, "current-second")
    unchanged_inserted = refresh_derived_snapshots(AS_OF, "current-unchanged")
    future = _history_result(
        reference_date=date(2026, 9, 28),
        value=-100.0,
        as_of=AS_OF + timedelta(days=1),
    )
    stale = _history_result(
        reference_date=date(2010, 1, 1),
        value=-100.0,
        as_of=datetime(2010, 1, 2, 12, tzinfo=timezone.utc),
    )
    assert insert_snapshot(
        future,
        calculation_cycle_id="future-history",
        origin=SnapshotOrigin.TEST_FIXTURE,
    )
    assert insert_snapshot(
        stale,
        calculation_cycle_id="stale-history",
        origin=SnapshotOrigin.TEST_FIXTURE,
    )
    noisy_inserted = refresh_derived_snapshots(AS_OF, "current-noisy")

    # Then
    current_snapshots = [
        snapshot
        for snapshot in query_snapshot_history(REAL_RATE_KEY)
        if snapshot.result.as_of == AS_OF
    ]
    assert second_inserted == 1
    assert unchanged_inserted == 0
    assert noisy_inserted == 0
    assert len(current_snapshots) == 2
    assert len({snapshot.inputs_hash for snapshot in current_snapshots}) == 2
    assert all(
        dict(snapshot.result.dependency_fingerprints).get("historical_sample")
        for snapshot in current_snapshots
    )


def test_first_seen_fallback_creates_only_current_snapshot_not_bulk_backfill() -> None:
    # Given
    for index in range(3):
        reference = date(2024, 1, 1) + timedelta(days=index)
        _seed_focus(reference_date=reference.isoformat(), median=4.0 + index)
        _seed_di(
            trade_date=reference.isoformat(),
            maturity=(reference + timedelta(days=365)).isoformat(),
            symbol=f"DI1A{index}",
            rate=10.0 + index,
        )

    # When
    result = _refresh_result()

    # Then
    assert result.reference_date == "2024-01-03"
    assert len(query_snapshot_history(REAL_RATE_KEY)) == 1


def test_twenty_three_distinct_reference_dates_keep_classification_unavailable() -> None:
    # Given
    history_start = date(2024, 1, 1)
    for index in range(22):
        reference = history_start + timedelta(days=index)
        result = _history_result(
            reference_date=reference,
            value=float(index),
            as_of=datetime.combine(reference, datetime.min.time(), timezone.utc)
            + timedelta(hours=12),
        )
        assert insert_snapshot(
            result,
            calculation_cycle_id=f"history-{index}",
            origin=SnapshotOrigin.TEST_FIXTURE,
        )
    duplicate = _history_result(
        reference_date=history_start,
        value=999.0,
        as_of=datetime(2024, 2, 1, 12, tzinfo=timezone.utc),
    )
    assert insert_snapshot(
        duplicate,
        calculation_cycle_id="history-revision",
        origin=SnapshotOrigin.TEST_FIXTURE,
    )
    _seed_focus()
    _seed_di()

    # When
    result = _refresh_result()

    # Then
    assert result.value is not None
    assert result.historical_percentile is None
    assert result.classification is None
    assert result.classification_reason == "insufficient_history"


def test_twenty_four_distinct_reference_dates_enable_exact_midrank_classification() -> None:
    # Given
    history_start = date(2024, 1, 1)
    for index in range(23):
        reference = history_start + timedelta(days=index)
        result = _history_result(
            reference_date=reference,
            value=float(index),
            as_of=datetime.combine(reference, datetime.min.time(), timezone.utc)
            + timedelta(hours=12),
        )
        assert insert_snapshot(
            result,
            calculation_cycle_id=f"history-{index}",
            origin=SnapshotOrigin.TEST_FIXTURE,
        )
    _seed_focus()
    _seed_di()

    # When
    result = _refresh_result()

    # Then
    assert result.value is not None
    expected_less = sum(float(index) < result.value for index in range(23))
    assert result.historical_percentile == 100 * (expected_less + 0.5) / 24
    assert result.classification == "baixo"
    assert result.classification_reason is None


def test_percentile_uses_latest_snapshot_for_each_of_twenty_four_dates() -> None:
    # Given
    history_start = date(2024, 1, 1)
    duplicate_reference = history_start
    assert insert_snapshot(
        _history_result(
            reference_date=duplicate_reference,
            value=-100.0,
            as_of=datetime(2024, 1, 2, 12, tzinfo=timezone.utc),
        ),
        calculation_cycle_id="duplicate-old",
        origin=SnapshotOrigin.TEST_FIXTURE,
    )
    assert insert_snapshot(
        _history_result(
            reference_date=duplicate_reference,
            value=100.0,
            as_of=datetime(2024, 2, 1, 12, tzinfo=timezone.utc),
        ),
        calculation_cycle_id="duplicate-latest",
        origin=SnapshotOrigin.TEST_FIXTURE,
    )
    values = tuple(float(value) for value in range(-10, -1)) + tuple(
        float(value) for value in range(100, 113)
    )
    for offset, value in enumerate(values, start=1):
        reference = history_start + timedelta(days=offset)
        assert insert_snapshot(
            _history_result(
                reference_date=reference,
                value=value,
                as_of=datetime.combine(reference, datetime.min.time(), timezone.utc)
                + timedelta(hours=12),
            ),
            calculation_cycle_id=f"history-{offset}",
            origin=SnapshotOrigin.TEST_FIXTURE,
        )
    _seed_focus()
    _seed_di()

    # When
    result = _refresh_result()

    # Then
    assert result.historical_percentile == 100 * 9.5 / 24
    assert result.classification == "baixo"
    assert result.classification_reason is None


pytestmark = pytest.mark.usefixtures(isolated_database.__name__)
