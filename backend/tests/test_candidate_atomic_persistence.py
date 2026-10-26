import sqlite3
from pathlib import Path

import pytest

from db import sidra_store


@pytest.fixture()
def isolated_store(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    from db import store

    if store._conn is not None:
        store._conn.close()
    store._conn = None
    monkeypatch.setattr(store, "DB_PATH", tmp_path / "candidate-atomic.db")
    monkeypatch.setattr(store, "DATABASE_URL", "")
    store.init_db()
    yield store
    if store._conn is not None:
        store._conn.close()
    store._conn = None


def _origin(extraction_id: str = "operation-1") -> tuple[object, ...]:
    return (
        "origin-1", extraction_id, "bcb_sgs", "sgs:433",
        '{"reference_date":"2026-01-01","series_code":433}',
        "2026-01-01", "a" * 64, "candidate", "bcb_sgs:csv:433",
        "c" * 64, None, "2026-01-01T12:00:00+00:00",
    )


def _receipt(extraction_id: str = "operation-1") -> tuple[object, ...]:
    return (
        extraction_id, "candidate", "bcb_sgs:csv:433", 1, "c" * 64,
        "2026-01-01T12:00:00+00:00",
    )


def test_sgs_business_vintage_origin_and_receipt_rollback_together(isolated_store):
    isolated_store._execute(
        "CREATE TRIGGER reject_origin BEFORE INSERT ON source_observation_origins "
        "BEGIN SELECT RAISE(ABORT, 'injected origin failure'); END"
    )

    with pytest.raises(sqlite3.IntegrityError):
        isolated_store.upsert_sgs(
            433,
            [{"data": "01/01/2026", "valor": "1.5"}],
            first_seen_at="2026-01-01T12:00:00+00:00",
            origin_rows=[_origin()],
            commit_receipt=_receipt(),
        )

    assert isolated_store._query("SELECT * FROM sgs WHERE series_code = 433") == []
    assert isolated_store._query("SELECT * FROM observation_vintages WHERE provider = 'bcb_sgs'") == []
    assert isolated_store._query("SELECT * FROM source_observation_origins") == []
    assert isolated_store._query("SELECT * FROM source_acquisition_commits") == []


def test_sidra_business_observations_origin_and_receipt_rollback_together(isolated_store):
    observation = {
        "dataset_key": "pim_industria",
        "table_id": 8888,
        "variable_id": 12607,
        "classification_id": 544,
        "category_id": 129314,
        "territory_level": "N1",
        "territory_code": "1",
        "reference_period": "202601",
        "reference_label": "janeiro 2026",
        "value": 105.2,
        "unit": "index",
        "base_version": "2022=100",
        "source_published_at": None,
    }
    isolated_store._execute(
        "CREATE TRIGGER reject_origin BEFORE INSERT ON source_observation_origins "
        "BEGIN SELECT RAISE(ABORT, 'injected origin failure'); END"
    )

    with pytest.raises(sqlite3.IntegrityError):
        sidra_store.insert_sidra_observations(
            [observation],
            first_seen_at="2026-01-01T12:00:00+00:00",
            origin_rows=[_origin()],
            commit_receipt=_receipt(),
        )

    assert isolated_store._query("SELECT * FROM sidra_observations") == []
    assert isolated_store._query("SELECT * FROM source_observation_origins") == []
    assert isolated_store._query("SELECT * FROM source_acquisition_commits") == []


def test_sidra_selected_batch_commits_receipt_and_provenance_together(isolated_store):
    observation = {
        "dataset_key": "pim_industria",
        "table_id": 8888,
        "variable_id": 12607,
        "classification_id": 544,
        "category_id": 129314,
        "territory_level": "N1",
        "territory_code": "1",
        "reference_period": "202601",
        "reference_label": "janeiro 2026",
        "value": 105.2,
        "unit": "index",
        "base_version": "2022=100",
        "source_published_at": None,
    }
    sidra_store.insert_sidra_observations(
        [observation],
        first_seen_at="2026-01-01T12:00:00+00:00",
        origin_rows=[_origin()],
        commit_receipt=_receipt(),
    )

    assert len(isolated_store._query("SELECT * FROM sidra_observations")) == 1
    assert len(isolated_store._query("SELECT * FROM source_observation_origins")) == 1
    receipt = isolated_store.source_acquisition_commit("operation-1")
    assert receipt is not None
    assert receipt["selected_route"] == "candidate"


def test_tr_same_value_candidate_appends_origin_without_new_vintage(isolated_store):
    existing = [{
        "data": "01/01/2026",
        "dataFim": "31/01/2026",
        "valor": "1.25",
    }]
    isolated_store.upsert_tr_periods(
        existing, first_seen_at="2026-02-01T00:00:00+00:00"
    )
    origin = (
        "origin-tr", "operation-tr", "bcb_sgs", "sgs:226",
        '{"period_end":"2026-01-31","period_start":"2026-01-01","series_code":226}',
        "2026-01-01", "a" * 64, "candidate", "bcb_sgs:csv:226",
        "c" * 64, None, "2026-03-01T00:00:00+00:00",
    )
    receipt = (
        "operation-tr", "candidate", "bcb_sgs:csv:226", 1, "c" * 64,
        "2026-03-01T00:00:00+00:00",
    )

    inserted = isolated_store.upsert_tr_periods(
        existing,
        first_seen_at="2026-03-01T00:00:00+00:00",
        origin_rows=[origin],
        commit_receipt=receipt,
    )

    assert inserted == 0
    assert len(isolated_store._query("SELECT * FROM tr_period_observations")) == 1
    assert len(isolated_store._query("SELECT * FROM observation_vintages WHERE series_key = 'tr'")) == 1
    assert len(isolated_store._query("SELECT * FROM source_observation_origins")) == 1
    assert isolated_store.source_acquisition_commit("operation-tr") is not None


def test_operation_receipt_replay_is_idempotent_but_payload_conflict_fails(isolated_store):
    observation = {
        "dataset_key": "pim_industria",
        "table_id": 8888,
        "variable_id": 12607,
        "classification_id": 544,
        "category_id": 129314,
        "territory_level": "N1",
        "territory_code": "1",
        "reference_period": "202601",
        "reference_label": "janeiro 2026",
        "value": 105.2,
        "unit": "index",
        "base_version": "2022=100",
        "source_published_at": None,
    }
    sidra_store.insert_sidra_observations(
        [observation], first_seen_at="2026-01-01T12:00:00+00:00",
        origin_rows=[_origin()], commit_receipt=_receipt(),
    )
    # Exact operation replay must not append any additional current data or origins.
    sidra_store.insert_sidra_observations(
        [observation], first_seen_at="2026-01-01T12:00:00+00:00",
        origin_rows=[_origin()], commit_receipt=_receipt(),
    )
    assert len(isolated_store._query("SELECT * FROM sidra_observations")) == 1
    assert len(isolated_store._query("SELECT * FROM source_observation_origins")) == 1

    conflicting_receipt = (*_receipt()[:4], "e" * 64, *_receipt()[5:])
    changed = {**observation, "value": 106.0}
    with pytest.raises(ValueError, match="different selected batch receipt"):
        sidra_store.insert_sidra_observations(
            [changed], first_seen_at="2026-01-01T12:00:00+00:00",
            origin_rows=[_origin()], commit_receipt=conflicting_receipt,
        )
    assert len(isolated_store._query("SELECT * FROM sidra_observations")) == 1
    assert len(isolated_store._query("SELECT * FROM source_observation_origins")) == 1
