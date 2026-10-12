from dataclasses import replace
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from db import store
from derived import inflation_target_inputs
from derived.inflation_targets import INFLATION_TARGET_RULES

FIRST_SEEN = datetime(2026, 10, 8, 9, tzinfo=timezone.utc)


@pytest.fixture(autouse=True)
def isolated_database(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    if store._conn is not None:
        store._conn.close()
    store._conn = None
    monkeypatch.setattr(store, "DB_PATH", tmp_path / "target-observations.db")
    monkeypatch.setattr(store, "DATABASE_URL", "")
    store.init_db()
    yield
    if store._conn is not None:
        store._conn.close()
    store._conn = None


def test_observation_persists_every_rule_with_true_local_first_seen(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # Given
    monkeypatch.setattr(inflation_target_inputs, "_now", lambda: FIRST_SEEN)

    # When
    count = inflation_target_inputs.observe_inflation_target_rules()

    # Then
    rows = store.query_observation_vintages("bcb_cmn", "inflation_target_rules")
    assert count == len(INFLATION_TARGET_RULES) == 11
    assert len(rows) == 11
    assert all(row["source_published_at"] is None for row in rows)
    assert all(row["first_seen_at"] == FIRST_SEEN.isoformat() for row in rows)
    assert all(row["available_at"] == FIRST_SEEN.isoformat() for row in rows)


def test_reobservation_after_database_reopen_preserves_original_first_seen(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # Given
    monkeypatch.setattr(inflation_target_inputs, "_now", lambda: FIRST_SEEN)
    inflation_target_inputs.observe_inflation_target_rules()
    store._conn.close()
    store._conn = None
    store.init_db()

    # When
    monkeypatch.setattr(inflation_target_inputs, "_now", lambda: FIRST_SEEN + timedelta(days=4))
    count = inflation_target_inputs.observe_inflation_target_rules()

    # Then
    rows = store.query_observation_vintages("bcb_cmn", "inflation_target_rules")
    assert count == 11
    assert len(rows) == 11
    assert all(row["first_seen_at"] == FIRST_SEEN.isoformat() for row in rows)


def test_rule_revision_appends_a_distinct_target_observation(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # Given
    monkeypatch.setattr(inflation_target_inputs, "_now", lambda: FIRST_SEEN)
    inflation_target_inputs.observe_inflation_target_rules()
    revised = replace(INFLATION_TARGET_RULES[-1], table_version="1.0.1", center_pct=3.1)
    revised_seen = FIRST_SEEN + timedelta(days=1)

    # When
    monkeypatch.setattr(inflation_target_inputs, "_now", lambda: revised_seen)
    count = inflation_target_inputs.observe_inflation_target_rules((revised,))

    # Then
    rows = store.query_observation_vintages("bcb_cmn", "inflation_target_rules")
    revisions = [row for row in rows if row["reference_period"] == "2025-01-01"]
    assert count == 1
    assert len(rows) == 12
    assert len(revisions) == 2
    assert revisions[0]["first_seen_at"] == FIRST_SEEN.isoformat()
    assert revisions[1]["first_seen_at"] == revised_seen.isoformat()
    assert revisions[0]["payload_hash"] != revisions[1]["payload_hash"]


def test_target_observations_respect_cutoff_equality(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # Given
    monkeypatch.setattr(inflation_target_inputs, "_now", lambda: FIRST_SEEN)
    inflation_target_inputs.observe_inflation_target_rules()

    # When
    before = inflation_target_inputs.load_inflation_target_observations(
        as_of=FIRST_SEEN - timedelta(microseconds=1)
    )
    at_cutoff = inflation_target_inputs.load_inflation_target_observations(as_of=FIRST_SEEN)

    # Then
    assert before == ()
    assert len(at_cutoff) == 11
    assert all(item.first_seen_at == FIRST_SEEN for item in at_cutoff)
    assert all(item.available_at == FIRST_SEEN for item in at_cutoff)


def test_invalid_rule_batch_is_rejected_before_any_rows_are_written() -> None:
    # Given
    invalid = replace(INFLATION_TARGET_RULES[-1], center_pct=float("nan"))
    rules = (INFLATION_TARGET_RULES[0], invalid)

    # When
    with pytest.raises(ValueError):
        inflation_target_inputs.observe_inflation_target_rules(rules)

    # Then
    assert store.query_observation_vintages("bcb_cmn", "inflation_target_rules") == []


def test_postgres_batch_uses_native_parameter_placeholders(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # Given
    captured: list[tuple[str, list[tuple[str | int | float | bool | None, ...]]]] = []

    def capture(sql: str, rows: list[tuple[str | int | float | bool | None, ...]]) -> None:
        captured.append((sql, rows))

    monkeypatch.setattr(inflation_target_inputs, "_now", lambda: FIRST_SEEN)
    monkeypatch.setattr(store, "uses_postgres", lambda: True)
    monkeypatch.setattr(store, "execute_many", capture)

    # When
    count = inflation_target_inputs.observe_inflation_target_rules()

    # Then
    assert count == 11
    assert len(captured) == 1
    sql, rows = captured[0]
    assert sql.count("%s") == 11
    assert "?" not in sql
    assert len(rows) == 11
