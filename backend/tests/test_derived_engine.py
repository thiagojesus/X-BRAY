from datetime import datetime, timezone
from pathlib import Path

import pytest

from derived.engine import DerivationSpec, calculate
from derived.models import DerivedInput, UnavailableReason


def test_red_xbr_501_migration_creates_immutable_snapshot_ledger(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from db import store

    if store._conn is not None:
        store._conn.close()
    store._conn = None
    monkeypatch.setattr(store, "DB_PATH", tmp_path / "derived-red.db")
    monkeypatch.setattr(store, "DATABASE_URL", "")
    store.init_db()

    try:
        tables = {
            row["name"]
            for row in store._query("SELECT name FROM sqlite_master WHERE type = 'table'")
        }
        assert "derived_snapshots" in tables, "[RED XBR-501] immutable snapshot ledger is missing"
    finally:
        if store._conn is not None:
            store._conn.close()
        store._conn = None


def _input(*, available_at: datetime) -> DerivedInput:
    return DerivedInput(
        key="series_a",
        value=10.0,
        unit="pct_annual",
        reference_period="2026-08-01",
        available_at=available_at,
        source="fixture",
    )


def test_calculation_is_deterministic_and_keeps_unrounded_value() -> None:
    as_of = datetime(2026, 9, 27, 12, tzinfo=timezone.utc)
    spec = DerivationSpec(
        key="sample",
        unit="ratio",
        methodology_version="1.0.0",
        formula="series_a / 3",
        calculate=lambda inputs: inputs[0].value / 3,
    )

    first = calculate(spec, (_input(available_at=as_of),), as_of)
    second = calculate(spec, (_input(available_at=as_of),), as_of)

    assert first == second
    assert first.value == 10.0 / 3
    assert first.reason is None


def test_future_input_is_explicitly_unavailable() -> None:
    as_of = datetime(2026, 9, 27, 12, tzinfo=timezone.utc)
    spec = DerivationSpec(
        key="sample",
        unit="ratio",
        methodology_version="1.0.0",
        formula="series_a",
        calculate=lambda inputs: inputs[0].value,
    )

    result = calculate(
        spec,
        (_input(available_at=datetime(2026, 9, 27, 13, tzinfo=timezone.utc)),),
        as_of,
    )

    assert result.value is None
    assert result.reason is UnavailableReason.INPUT_NOT_AVAILABLE


def test_naive_timestamps_are_rejected() -> None:
    naive = datetime.now(timezone.utc).replace(tzinfo=None)
    with pytest.raises(ValueError, match="timezone"):
        _input(available_at=naive)


def test_non_finite_result_is_explicitly_unavailable() -> None:
    as_of = datetime(2026, 9, 27, 12, tzinfo=timezone.utc)
    spec = DerivationSpec(
        key="sample",
        unit="ratio",
        methodology_version="1.0.0",
        formula="invalid",
        calculate=lambda _inputs: float("nan"),
    )

    result = calculate(spec, (_input(available_at=as_of),), as_of)

    assert result.value is None
    assert result.reason is UnavailableReason.NON_FINITE_RESULT
