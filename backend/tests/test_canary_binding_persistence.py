from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest

from db import sidra_store, store


@pytest.fixture()
def isolated_store(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    from db import store

    if store._conn is not None:
        store._conn.close()
    store._conn = None
    monkeypatch.setattr(store, "DB_PATH", tmp_path / "canary-binding.db")
    monkeypatch.setattr(store, "DATABASE_URL", "")
    store.init_db()
    yield store
    if store._conn is not None:
        store._conn.close()
    store._conn = None


def _binding(
    operation_id: str = "canary-op-1",
    *,
    as_of: str = "2026-01-31",
    plan_hash: str = "a" * 64,
    proof_hash: str = "b" * 64,
) -> store.CanaryBinding:
    return store.CanaryBinding(
        operation_id,
        "bcb_sgs:csv:433",
        as_of,
        plan_hash,
        proof_hash,
        "source-contract-v1",
        "c" * 64,
        "d" * 64,
        "2026-01-31",
        "2026-01-31T12:00:00+00:00",
        2,
        1,
        0,
        "2026-02-01T00:00:00+00:00",
    )


def _receipt(operation_id: str = "canary-op-1") -> tuple[object, ...]:
    return (
        operation_id,
        "candidate",
        "bcb_sgs:csv:433",
        1,
        "e" * 64,
        "2026-02-01T00:00:00+00:00",
    )


def _origin(operation_id: str = "canary-op-1") -> tuple[object, ...]:
    return (
        "origin-" + operation_id,
        operation_id,
        "bcb_sgs",
        "sgs:433",
        '{"reference_date":"2026-01-31","series_code":433}',
        "2026-01-31",
        "f" * 64,
        "candidate",
        "bcb_sgs:csv:433",
        "e" * 64,
        None,
        "2026-02-01T00:00:00+00:00",
        "source-contract-v1",
        "c" * 64,
        "d" * 64,
        "b" * 64,
    )


def test_sgs_binding_receipt_business_vintage_and_origin_commit_atomically(isolated_store):
    isolated_store.upsert_sgs(
        433,
        [{"data": "31/01/2026", "valor": "1.5"}],
        first_seen_at="2026-02-01T00:00:00+00:00",
        origin_rows=[_origin()],
        commit_receipt=_receipt(),
        canary_binding=_binding(),
    )

    assert len(isolated_store._query("SELECT * FROM sgs WHERE series_code = 433")) == 1
    assert len(isolated_store._query("SELECT * FROM observation_vintages WHERE series_key = 'ipca'")) == 1
    origin = isolated_store._query("SELECT * FROM source_observation_origins")[0]
    assert origin["contract_version"] == "source-contract-v1"
    assert origin["profile_semantic_sha256"] == "c" * 64
    assert origin["parser_semantic_sha256"] == "d" * 64
    assert origin["proof_sha256"] == "b" * 64
    assert isolated_store.source_canary_binding("canary-op-1")["plan_sha256"] == "a" * 64
    assert isolated_store.source_acquisition_commit("canary-op-1")["selected_route"] == "candidate"


def test_sidra_binding_receipt_observation_and_origin_commit_atomically(isolated_store):
    observation = {
        "dataset_key": "pim_industria", "table_id": 8888, "variable_id": 12607,
        "classification_id": 544, "category_id": 129314, "territory_level": "N1",
        "territory_code": "1", "reference_period": "202601", "reference_label": "janeiro 2026",
        "value": 105.2, "unit": "index", "base_version": "2022=100", "source_published_at": None,
    }
    sidra_store.insert_sidra_observations(
        [observation], origin_rows=[_origin()], commit_receipt=_receipt(), canary_binding=_binding(),
    )

    assert len(isolated_store._query("SELECT * FROM sidra_observations")) == 1
    assert len(isolated_store._query("SELECT * FROM source_observation_origins")) == 1
    assert isolated_store.source_canary_binding("canary-op-1")["candidate_id"] == "bcb_sgs:csv:433"


def test_binding_origin_receipt_and_business_rows_rollback_together(isolated_store):
    isolated_store._execute(
        "CREATE TRIGGER reject_origin BEFORE INSERT ON source_observation_origins "
        "BEGIN SELECT RAISE(ABORT, 'injected origin failure'); END"
    )
    with pytest.raises(sqlite3.IntegrityError):
        isolated_store.upsert_sgs(
            433, [{"data": "31/01/2026", "valor": "1.5"}],
            origin_rows=[_origin()], commit_receipt=_receipt(), canary_binding=_binding(),
        )

    assert isolated_store._query("SELECT * FROM sgs WHERE series_code = 433") == []
    assert isolated_store._query("SELECT * FROM observation_vintages") == []
    assert isolated_store._query("SELECT * FROM source_observation_origins") == []
    assert isolated_store.source_acquisition_commit("canary-op-1") is None
    assert isolated_store.source_canary_binding("canary-op-1") is None


def test_exact_binding_replay_is_noop_and_changed_plan_or_proof_conflicts(isolated_store):
    args = {
        "first_seen_at": "2026-02-01T00:00:00+00:00",
        "origin_rows": [_origin()],
        "commit_receipt": _receipt(),
        "canary_binding": _binding(),
    }
    isolated_store.upsert_sgs(433, [{"data": "31/01/2026", "valor": "1.5"}], **args)
    isolated_store.upsert_sgs(433, [{"data": "31/01/2026", "valor": "1.5"}], **args)
    assert len(isolated_store._query("SELECT * FROM sgs WHERE series_code = 433")) == 1
    assert len(isolated_store._query("SELECT * FROM source_observation_origins")) == 1

    for changed, changed_origin in (
        (_binding(as_of="2026-01-30"), _origin()),
        (_binding(plan_hash="9" * 64), _origin()),
        (_binding(proof_hash="8" * 64), (*_origin()[:15], "8" * 64)),
    ):
        with pytest.raises(ValueError, match="different canary binding"):
            isolated_store.upsert_sgs(
                433, [{"data": "30/01/2026", "valor": "2.0"}],
                origin_rows=[changed_origin], commit_receipt=_receipt(), canary_binding=changed,
            )
    assert len(isolated_store._query("SELECT * FROM sgs WHERE series_code = 433")) == 1


def test_legacy_origins_keep_attestation_null_and_v16_preserves_old_rows(isolated_store):
    isolated_store.upsert_sgs(
        433, [{"data": "31/01/2026", "valor": "1.5"}], origin_rows=[_origin()[:12]],
        commit_receipt=_receipt(),
    )
    legacy = isolated_store._query("SELECT contract_version, profile_semantic_sha256, parser_semantic_sha256, proof_sha256 FROM source_observation_origins")[0]
    assert tuple(legacy.values()) == (None, None, None, None)
    assert isolated_store._query("SELECT operation_id FROM source_canary_bindings") == []


def test_canary_binding_cannot_be_committed_without_receipt_and_matching_origin(isolated_store):
    with pytest.raises(ValueError, match="require an acquisition receipt"):
        isolated_store.upsert_sgs(
            433, [{"data": "31/01/2026", "valor": "1.5"}], canary_binding=_binding(),
        )
    with pytest.raises(ValueError, match="attestation does not match"):
        isolated_store.upsert_sgs(
            433, [{"data": "31/01/2026", "valor": "1.5"}],
            origin_rows=[(*_origin()[:15], "9" * 64)],
            commit_receipt=_receipt(), canary_binding=_binding(),
        )
    assert isolated_store._query("SELECT * FROM sgs WHERE series_code = 433") == []
    assert isolated_store._query("SELECT * FROM source_canary_bindings") == []
