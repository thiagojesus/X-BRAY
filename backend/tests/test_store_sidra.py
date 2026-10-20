from pathlib import Path

import pytest


@pytest.fixture()
def sidra_store(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    from db import store

    if store._conn is not None:
        store._conn.close()
    store._conn = None
    monkeypatch.setattr(store, "DB_PATH", tmp_path / "sidra.db")
    monkeypatch.setattr(store, "DATABASE_URL", "")
    store.init_db()
    yield store
    if store._conn is not None:
        store._conn.close()
    store._conn = None


def _observation(value: float) -> dict[str, object]:
    return {
        "dataset_key": "pim_industria",
        "table_id": 8888,
        "variable_id": 12607,
        "classification_id": 544,
        "category_id": 129314,
        "territory_level": "N1",
        "territory_code": "1",
        "reference_period": "202507",
        "reference_label": "julho 2025",
        "value": value,
        "unit": "index",
        "base_version": "2022=100",
        "source_published_at": None,
    }


def test_sidra_migration_is_idempotent_and_has_required_columns(sidra_store) -> None:
    sidra_store.init_db()
    versions = sidra_store._query(
        "SELECT version FROM schema_migrations ORDER BY version"
    )
    columns = {
        row["name"]
        for row in sidra_store._query("PRAGMA table_info(sidra_observations)")
    }

    assert versions == [
        {"version": "001_observability"},
        {"version": "002_sidra_observations"},
        {"version": "003_derived_snapshots"},
        {"version": "004_macro_score_snapshots"},
        {"version": "005_resumable_refresh"},
        {"version": "006_retrospective_macro_history"},
        {"version": "007_retrospective_di"},
        {"version": "008_di_maturity_provenance"},
        {"version": "009_b3_instrument_snapshots"},
        {"version": "010_production_data_imports"},
    ]
    assert {
        "dataset_key",
        "table_id",
        "variable_id",
        "classification_id",
        "category_id",
        "territory_level",
        "territory_code",
        "reference_period",
        "value",
        "unit",
        "base_version",
        "source_published_at",
        "first_seen_at",
        "available_at",
        "payload_hash",
    } <= columns


def test_revision_vintages_are_append_only_current_and_as_of_safe(sidra_store) -> None:
    observed_a = "2026-08-01T12:00:00+00:00"
    observed_b = "2026-09-01T12:00:00+00:00"
    sidra_store.insert_sidra_observations(
        [_observation(100.0)], first_seen_at=observed_a
    )
    sidra_store.insert_sidra_observations(
        [_observation(101.0)], first_seen_at=observed_b
    )
    sidra_store.insert_sidra_observations(
        [_observation(101.0)], first_seen_at="2026-09-02T12:00:00+00:00"
    )

    history = sidra_store.query_sidra_history("pim_industria")
    current = sidra_store.query_sidra_current(("pim_industria",))
    historical = sidra_store.query_sidra_current(
        ("pim_industria",),
        as_of="2026-08-15T00:00:00+00:00",
    )

    assert len(history) == 2
    assert [row["value"] for row in history] == [100.0, 101.0]
    assert current["pim_industria"][0]["value"] == 101.0
    assert historical["pim_industria"][0]["value"] == 100.0
    assert history[1]["first_seen_at"] == observed_b
    assert all(row["source_published_at"] is None for row in history)
    assert all(row["available_at"] == row["first_seen_at"] for row in history)


def test_exact_revision_timestamp_controls_availability_when_present(
    sidra_store,
) -> None:
    observation = {
        **_observation(102.0),
        "source_published_at": "2026-07-30T09:00:00+00:00",
    }
    sidra_store.insert_sidra_observations(
        [observation],
        first_seen_at="2026-08-01T12:00:00+00:00",
    )

    row = sidra_store.query_sidra_history("pim_industria")[0]
    assert row["available_at"] == "2026-07-30T09:00:00+00:00"


@pytest.mark.parametrize("field", ["dataset_key", "reference_period", "unit", "value"])
def test_invalid_sidra_observation_is_rejected(sidra_store, field: str) -> None:
    observation = _observation(100.0)
    observation[field] = float("nan") if field == "value" else ""

    with pytest.raises(ValueError):
        sidra_store.insert_sidra_observations([observation])

    assert sidra_store.query_sidra_history("pim_industria") == []


@pytest.mark.parametrize("reference_period", ["202600", "202613", "000001"])
def test_invalid_calendar_period_is_rejected(
    sidra_store, reference_period: str
) -> None:
    observation = _observation(100.0)
    observation["reference_period"] = reference_period

    with pytest.raises(ValueError, match="reference_period"):
        sidra_store.insert_sidra_observations([observation])

    assert sidra_store.query_sidra_history("pim_industria") == []
