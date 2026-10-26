from __future__ import annotations

import hashlib
import json
import os
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone
from types import MappingProxyType
from typing import ClassVar
from urllib.parse import urlparse
from uuid import uuid4

import psycopg
import pytest
from fastapi.testclient import TestClient
from psycopg.conninfo import make_conninfo

from candidate_eligibility import (
    CandidateApproval,
    parser_semantic_sha256,
    profile_semantic_sha256,
)
from db import store
from main import app
from source_candidates import CandidateStatus, get_candidate_profile

pytestmark = pytest.mark.skipif(
    not os.environ.get("TEST_DATABASE_URL"), reason="requires isolated PostgreSQL 17 TEST_DATABASE_URL"
)


@pytest.fixture()
def pg_canary(monkeypatch: pytest.MonkeyPatch):
    dsn = os.environ["TEST_DATABASE_URL"]
    parsed = urlparse(dsn)
    if parsed.hostname not in {"127.0.0.1", "localhost", "::1"} or parsed.path != "/xbray_test":
        pytest.fail("canary PostgreSQL integration requires loopback database xbray_test")
    with psycopg.connect(dsn) as connection:
        version, name = connection.execute("SELECT version(), current_database()").fetchone()
        assert "PostgreSQL 17" in version
        assert name == "xbray_test"
        schema = f"canary_{uuid4().hex}"
        application_name = f"stagec_{schema}"
        connection.execute(f'CREATE SCHEMA "{schema}"')
    scoped_dsn = make_conninfo(
        dsn,
        options=f"-c search_path={schema}",
        application_name=application_name,
    )
    if store._conn is not None:
        store._conn.close()
    store._conn = None
    pg_conn = getattr(store._pg_local, "conn", None)
    if pg_conn is not None:
        pg_conn.close()
        store._pg_local.conn = None
    monkeypatch.setattr(store, "DATABASE_URL", scoped_dsn)
    store.init_db()
    monkeypatch.setenv("XBRAY_ENV", "local")
    monkeypatch.setenv("SOURCE_FALLBACK_MODE", "canary")
    monkeypatch.setattr("main.init_db", lambda: None)
    from routes import source_candidates

    monkeypatch.setattr(source_candidates.store, "source_access_states", dict)
    monkeypatch.setattr(
        source_candidates.store,
        "source_candidate_activity",
        lambda _limit: {"extractions": [], "attempts": [], "origins": []},
    )
    monkeypatch.setattr(
        source_candidates,
        "readiness_snapshot",
        lambda **_kwargs: {"candidates": []},
    )
    yield schema
    pg_conn = getattr(store._pg_local, "conn", None)
    if pg_conn is not None:
        pg_conn.close()
        store._pg_local.conn = None
    with psycopg.connect(dsn) as connection:
        connection.execute(
            "SELECT pg_terminate_backend(pid) FROM pg_stat_activity "
            "WHERE datname = current_database() AND application_name = %s "
            "AND pid <> pg_backend_pid()",
            (application_name,),
        )
        connection.execute(f'DROP SCHEMA "{schema}" CASCADE')


class _RawResponse:
    status_code = 200
    headers: ClassVar[dict[str, str]] = {"content-type": "application/json"}

    def __init__(self, body: bytes, url: str):
        self.content = body
        self.url = url

    def raise_for_status(self) -> None:
        return None


def test_postgres_advisory_lock_survives_store_reconnect_and_releases_after_error(pg_canary):
    operation_id = f"lock-{uuid4().hex[:16]}"
    lock_key = int.from_bytes(hashlib.sha256(operation_id.encode("utf-8")).digest()[:8], "big", signed=True)
    with pytest.raises(ValueError, match="expected operation failure"), store.canary_operation_lock(operation_id):
        assert store.source_acquisition_commit(operation_id, reconnect=True) is None
        raise ValueError("expected operation failure")
    with psycopg.connect(os.environ["TEST_DATABASE_URL"]) as connection:
        acquired = connection.execute("SELECT pg_try_advisory_lock(%s)", (lock_key,)).fetchone()[0]
        assert acquired is True
        connection.execute("SELECT pg_advisory_unlock(%s)", (lock_key,))


def _qualification(candidate_id: str) -> tuple[object, CandidateApproval]:
    profile = get_candidate_profile(candidate_id)
    profile = profile.__class__(
        **{
            **{field: getattr(profile, field) for field in profile.__dataclass_fields__},
            "status": CandidateStatus.ELIGIBLE,
            "enabled_for_ingestion": True,
            "code_reviewed": True,
            "approved_proof_sha256": "d" * 64,
            "license_status": "verified",
        }
    )
    approval = CandidateApproval(
        candidate_id, profile.source_contract_version, profile_semantic_sha256(profile),
        parser_semantic_sha256(candidate_id), "d" * 64, 10, "2026-10-10", True, True, True,
    )
    return profile, approval


def _sidra_payloads() -> tuple[bytes, bytes]:
    from datafetchers.ibge_sidra import SIDRA_DATASETS

    spec = SIDRA_DATASETS["pim_industria"]
    period = "202610"
    header = {
        key: label
        for key, label in {
            "NC": "Nível Territorial", "NN": "Nível Territorial (Nome)",
            "MC": "Unidade de Medida", "MN": "Unidade de Medida (Nome)",
            "V": "Valor", "D1C": "Brasil", "D1N": "Brasil",
            "D2C": "Variável", "D2N": "Variável (Nome)",
            "D3C": "Mês", "D3N": "Mês (Código)",
            "D4C": "Atividade", "D4N": "Atividade (Código)",
        }.items()
    }
    primary = [header, {
        "NC": "1", "NN": "Brasil", "MC": spec.unit_code, "MN": spec.unit_label,
        "V": "12,5", "D1C": "1", "D1N": "Brasil", "D2C": str(spec.variable_id),
        "D2N": spec.variable_label, "D3C": period, "D3N": "outubro 2026",
        "D4C": str(spec.category_id), "D4N": spec.category_label,
    }]
    candidate = [{
        "id": str(spec.variable_id), "variavel": spec.variable_label, "unidade": spec.unit_label,
        "resultados": [{
            "classificacoes": [{"id": str(spec.classification_id), "categoria": {str(spec.category_id): spec.category_label}}],
            "series": [{"localidade": {"id": "1", "nome": "Brasil", "nivel": {"id": "N1", "nome": "Brasil"}},
                        "serie": {period: "12.5"}}],
        }],
    }]
    return json.dumps(primary).encode(), json.dumps(candidate).encode()


def _install_request_fixture(monkeypatch, candidate_id: str) -> list[str]:
    from routes import source_candidates

    profile, approval = _qualification(candidate_id)
    calls: list[str] = []
    monkeypatch.setattr(source_candidates, "get_candidate_profile", lambda _identifier: profile)
    monkeypatch.setattr(
        source_candidates,
        "readiness_snapshot",
        lambda **_kwargs: {
            "candidates": [{"candidate_id": profile.candidate_id, "ready": True, "blockers": []}]
        },
    )
    monkeypatch.setattr(source_candidates, "APPROVED_CANDIDATE_APPROVALS", MappingProxyType({candidate_id: approval}))
    from ingestion_policy import current_retry_budget

    if profile.source.value == "bcb_sgs":
        primary_json: bytes | None = None
        candidate_csv: bytes | None = None

        def response(url: str, **kwargs):
            nonlocal primary_json, candidate_csv
            calls.append(url)
            budget = current_retry_budget()
            assert budget is not None
            budget.reserve_request()
            params = kwargs.get("params", {})
            if params.get("formato") == "json":
                end = datetime.strptime(params["dataFinal"], "%d/%m/%Y").replace(tzinfo=timezone.utc).date()
                start = datetime.strptime(params["dataInicial"], "%d/%m/%Y").replace(tzinfo=timezone.utc).date()
                points = []
                day = start
                while day <= end:
                    points.append({"data": day.strftime("%d/%m/%Y"), "valor": str(day.day)})
                    day += timedelta(days=1)
                primary_json = json.dumps(points).encode()
                return _RawResponse(primary_json, url)
            end = datetime.strptime(params["dataFinal"], "%d/%m/%Y").replace(tzinfo=timezone.utc).date()
            start = datetime.strptime(params["dataInicial"], "%d/%m/%Y").replace(tzinfo=timezone.utc).date()
            lines = ["data;valor"]
            day = start
            while day <= end:
                lines.append(f"{day:%d/%m/%Y};{day.day}")
                day += timedelta(days=1)
            candidate_csv = "\n".join(lines).encode()
            return _RawResponse(candidate_csv, url)

    else:
        primary_payload, candidate_payload = _sidra_payloads()

        def response(url: str, **kwargs):
            budget = current_retry_budget()
            assert budget is not None
            budget.reserve_request()
            calls.append(url)
            call_index = len(calls)
            body = primary_payload if call_index == 1 else candidate_payload
            return _RawResponse(body, url)

    monkeypatch.setattr("provider_http.get_provider_response", response)
    return calls


def _payload(candidate_id: str, operation_id: str, as_of: str = "2026-10-20") -> dict[str, object]:
    profile = get_candidate_profile(candidate_id)
    return {
        "candidate_id": candidate_id, "as_of": as_of, "dry_run": False,
        "confirm_write": True, "expected_proof_sha256": "d" * 64,
        "expected_contract_version": profile.source_contract_version, "operation_id": operation_id,
    }


@pytest.mark.parametrize("candidate_id", ["bcb_sgs:csv:433", "ibge_sidra:v3:pim_industria"])
def test_postgres_route_commit_replay_conflict_and_concurrent_idempotency(pg_canary, monkeypatch, candidate_id):
    calls = _install_request_fixture(monkeypatch, candidate_id)
    operation = f"pg-{uuid4().hex[:16]}"
    with TestClient(app) as client:
        response = client.post("/api/source-candidates/canary", json=_payload(candidate_id, operation))
        assert response.status_code == 200, response.text
        body = response.json()
        assert body["committed"] is True
        assert body["request_count"] == 2
        assert len(calls) == 2
        assert store.source_canary_binding(operation) is not None
        assert store.source_acquisition_commit(operation)["selected_route"] == "candidate"
        assert len(store._query("SELECT * FROM source_observation_origins WHERE extraction_id = ?", (operation,))) == body["origin_count"]
        assert len(store._query("SELECT * FROM sgs WHERE series_code = 433")) == 31 if candidate_id.startswith("bcb") else 1
        replay = client.post("/api/source-candidates/canary", json=_payload(candidate_id, operation))
        assert replay.status_code == 200
        assert replay.json()["status"] == "replayed_known_commit"
        assert len(calls) == 2
        conflict = client.post(
            "/api/source-candidates/canary",
            json=_payload(candidate_id, operation, "2026-10-19"),
        )
        assert conflict.status_code == 409

    calls = _install_request_fixture(monkeypatch, candidate_id)
    concurrent_id = f"pg-{uuid4().hex[:16]}"

    def post_same_id():
        with TestClient(app) as client:
            return client.post("/api/source-candidates/canary", json=_payload(candidate_id, concurrent_id))

    with ThreadPoolExecutor(max_workers=2) as pool:
        results = [future.result(timeout=30) for future in (pool.submit(post_same_id), pool.submit(post_same_id))]
    assert [item.status_code for item in results] == [200, 200]
    assert [item.json()["status"] for item in results].count("replayed_known_commit") == 1
    assert len(calls) == 2
    assert len(store._query("SELECT * FROM source_canary_bindings WHERE operation_id = ?", (concurrent_id,))) == 1
    assert len(store._query("SELECT * FROM source_acquisition_commits WHERE extraction_id = ?", (concurrent_id,))) == 1
    assert len(store._query("SELECT * FROM source_observation_origins WHERE extraction_id = ?", (concurrent_id,))) == 31 if candidate_id.startswith("bcb") else 1


@pytest.mark.parametrize("candidate_id", ["bcb_sgs:csv:433", "ibge_sidra:v3:pim_industria"])
def test_postgres_origin_trigger_rolls_back_business_vintage_receipt_and_binding(pg_canary, monkeypatch, candidate_id):
    _install_request_fixture(monkeypatch, candidate_id)
    operation = f"rollback-{uuid4().hex[:16]}"
    suffix = uuid4().hex[:12]
    function = f"reject_origin_{suffix}"
    trigger = f"reject_origin_trigger_{suffix}"
    store._execute(
        f"CREATE FUNCTION {function}() RETURNS trigger LANGUAGE plpgsql AS $$ "
        "BEGIN RAISE EXCEPTION 'injected origin insert failure'; END $$"
    )
    store._execute(
        f"CREATE TRIGGER {trigger} BEFORE INSERT ON source_observation_origins "
        f"FOR EACH ROW EXECUTE FUNCTION {function}()"
    )
    try:
        with TestClient(app) as client:
            response = client.post("/api/source-candidates/canary", json=_payload(candidate_id, operation))
        assert response.status_code == 502
        assert store.source_canary_binding(operation) is None
        assert store.source_acquisition_commit(operation) is None
        assert store._query("SELECT * FROM source_observation_origins WHERE extraction_id = ?", (operation,)) == []
        if candidate_id.startswith("bcb"):
            assert store._query("SELECT * FROM sgs WHERE series_code = 433") == []
            assert store._query("SELECT * FROM observation_vintages WHERE provider = 'bcb_sgs'") == []
        else:
            assert store._query("SELECT * FROM sidra_observations WHERE dataset_key = 'pim_industria'") == []
    finally:
        store._execute(f"DROP TRIGGER IF EXISTS {trigger} ON source_observation_origins")
        store._execute(f"DROP FUNCTION IF EXISTS {function}()")
