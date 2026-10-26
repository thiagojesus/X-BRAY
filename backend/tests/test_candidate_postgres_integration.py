from __future__ import annotations

import json
import os
from collections.abc import Iterator
from dataclasses import replace
from datetime import date, timedelta
from types import MappingProxyType
from urllib.parse import urlparse
from uuid import uuid4

import psycopg
import pytest

from candidate_acquisition import CandidateAcquisitionService
from candidate_eligibility import (
    CandidateApproval,
    parser_semantic_sha256,
    profile_semantic_sha256,
)
from source_candidates import CandidateStatus, get_candidate_profile

pytestmark = pytest.mark.skipif(
    not os.environ.get("TEST_DATABASE_URL"),
    reason="requires an explicitly supplied local PostgreSQL test database",
)


@pytest.fixture()
def postgres_store(monkeypatch: pytest.MonkeyPatch) -> Iterator[object]:
    from db import store

    dsn = os.environ["TEST_DATABASE_URL"]
    parsed = urlparse(dsn)
    if parsed.hostname not in {"127.0.0.1", "localhost", "::1"} or parsed.path != "/xbray_test":
        pytest.fail("PostgreSQL candidate tests require loopback xbray_test")
    with psycopg.connect(dsn) as connection:
        row = connection.execute("SELECT current_database(), current_user").fetchone()
        assert row == ("xbray_test", parsed.username)
    if store._conn is not None:
        store._conn.close()
    store._conn = None
    conn = getattr(store._pg_local, "conn", None)
    if conn is not None:
        conn.close()
        store._pg_local.conn = None
    monkeypatch.setattr(store, "DATABASE_URL", dsn)
    store.init_db()
    store._execute("DELETE FROM source_access_state")
    yield store
    conn = getattr(store._pg_local, "conn", None)
    if conn is not None:
        conn.close()
        store._pg_local.conn = None


def _origin(operation: str, source: str, contract: str, candidate: str) -> tuple[object, ...]:
    return (
        str(uuid4()), operation, source, contract,
        '{"reference_date":"2026-01-01","series_code":433}',
        "2026-01-01", "a" * 64, "candidate", candidate,
        "c" * 64, None, "2026-01-01T12:00:00+00:00",
    )


def _receipt(operation: str, candidate: str) -> tuple[object, ...]:
    return (
        operation, "candidate", candidate, 1, "c" * 64,
        "2026-01-01T12:00:00+00:00",
    )


def test_postgres_sgs_business_vintage_origins_and_receipt_rollback_together(
    postgres_store: object,
) -> None:
    store = postgres_store
    suffix = uuid4().hex[:10]
    operation = f"pg-op-{suffix}"
    candidate = f"bcb_sgs:csv:{suffix}"
    series_code = 900000 + int(suffix[:4], 16)
    trigger = f"reject_origin_{suffix}"
    store._execute(
        f"CREATE FUNCTION {trigger}() RETURNS trigger LANGUAGE plpgsql AS $$ "
        "BEGIN RAISE EXCEPTION 'injected origin insert failure'; END $$"
    )
    store._execute(
        f"CREATE TRIGGER {trigger} BEFORE INSERT ON source_observation_origins "
        f"FOR EACH ROW EXECUTE FUNCTION {trigger}()"
    )
    try:
        with pytest.raises(psycopg.Error):
            store.upsert_sgs(
                series_code,
                [{"data": "01/01/2026", "valor": "1.5"}],
                first_seen_at="2026-01-01T12:00:00+00:00",
                origin_rows=[_origin(operation, "bcb_sgs", f"sgs:{series_code}", candidate)],
                commit_receipt=_receipt(operation, candidate),
            )
    finally:
        store._execute(f"DROP TRIGGER IF EXISTS {trigger} ON source_observation_origins")
        store._execute(f"DROP FUNCTION IF EXISTS {trigger}()")

    assert store._query("SELECT * FROM sgs WHERE series_code = ?", (series_code,)) == []
    assert store._query(
        "SELECT * FROM observation_vintages WHERE provider = 'bcb_sgs' AND series_key = ?",
        (str(series_code),),
    ) == []
    assert store.source_acquisition_commit(operation) is None
    assert store._query(
        "SELECT * FROM source_observation_origins WHERE extraction_id = ?", (operation,)
    ) == []


def test_postgres_sidra_atomic_receipt_replay_and_conflict(postgres_store: object) -> None:
    from db.sidra_store import insert_sidra_observations

    store = postgres_store
    operation = f"pg-sidra-{uuid4()}"
    candidate = "ibge_sidra:v3:pim_industria"
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
        "value": 12345.6,
        "unit": "index",
        "base_version": "2022=100",
        "source_published_at": None,
    }
    origin = _origin(operation, "ibge_sidra", "sidra:pim_industria", candidate)
    receipt = _receipt(operation, candidate)
    insert_sidra_observations(
        [observation], first_seen_at="2026-01-01T12:00:00+00:00",
        origin_rows=[origin], commit_receipt=receipt,
    )
    insert_sidra_observations(
        [observation], first_seen_at="2026-01-01T12:00:00+00:00",
        origin_rows=[origin], commit_receipt=receipt,
    )
    assert store.source_acquisition_commit(operation) is not None
    assert len(store._query(
        "SELECT * FROM source_observation_origins WHERE extraction_id = ?", (operation,)
    )) == 1
    assert len(store._query(
        "SELECT * FROM sidra_observations WHERE dataset_key = 'pim_industria' "
        "AND reference_period = '202601' AND value = 12345.6"
    )) == 1

    conflicting = (*receipt[:4], "e" * 64, *receipt[5:])
    with pytest.raises(ValueError, match="different selected batch receipt"):
        insert_sidra_observations(
            [{**observation, "value": 999.0}],
            first_seen_at="2026-01-01T12:00:00+00:00",
            origin_rows=[origin], commit_receipt=conflicting,
        )
    assert store.source_acquisition_commit(operation)["source_payload_sha256"] == "c" * 64
    store._execute(
        "DELETE FROM sidra_observations WHERE dataset_key = 'pim_industria' "
        "AND reference_period = '202601' AND value = 12345.6"
    )


def _fixture_service(candidate_id: str, latest_reference: str) -> CandidateAcquisitionService:
    catalog = get_candidate_profile(candidate_id)
    proof = "d" * 64
    profile = replace(
        catalog, status=CandidateStatus.ELIGIBLE, enabled_for_ingestion=True,
        code_reviewed=True, approved_proof_sha256=proof, license_status="verified",
    )
    approval = CandidateApproval(
        candidate_id=candidate_id,
        contract_version=profile.source_contract_version,
        profile_semantic_sha256=profile_semantic_sha256(profile),
        parser_semantic_sha256=parser_semantic_sha256(candidate_id),
        proof_sha256=proof,
        distinct_references=10,
        latest_reference=latest_reference,
        code_reviewed=True,
        license_verified=True,
        complete_cohort_proven=True,
    )
    return CandidateAcquisitionService(
        enabled=True,
        profiles=MappingProxyType({candidate_id: profile}),
        approvals=MappingProxyType({candidate_id: approval}),
    )


def _mock_provider(
    monkeypatch: pytest.MonkeyPatch,
    module: object,
    *,
    primary_host: str,
    candidate_host: str,
    candidate_body: bytes,
) -> list[str]:
    from collections.abc import Mapping

    import httpx

    from provider_http import get_provider_response as real_get

    calls: list[str] = []

    def request(url: str, *, timeout: float, follow_redirects: bool,
                headers: Mapping[str, str] | None,
                params: Mapping[str, str] | None) -> httpx.Response:
        del timeout, follow_redirects, headers
        calls.append(url)
        del params
        primary_call = len(calls) == 1
        response_body = b"temporary failure" if primary_call else candidate_body
        return httpx.Response(
            502 if primary_call else 200,
            content=response_body,
            request=httpx.Request("GET", url),
        )

    def bounded(url: str, **kwargs: object):
        return real_get(url, request=request, **kwargs)

    monkeypatch.setattr(module, "get_provider_response", bounded)
    return calls


def test_postgres_real_sgs_producer_selects_and_persists_candidate(
    postgres_store: object, monkeypatch: pytest.MonkeyPatch
) -> None:
    import candidate_acquisition
    from datafetchers import bcb_sgs

    store = postgres_store
    service = _fixture_service("bcb_sgs:csv:433", "2026-01-10")
    monkeypatch.setattr(candidate_acquisition, "DEFAULT_CANDIDATE_ACQUISITION", service)
    start = date(2026, 1, 1)
    candidate_body = (
        "data;valor\n" + "\n".join(
            f"{(start + timedelta(days=i)):%d/%m/%Y};{i + 1},25" for i in range(10)
        ) + "\n"
    ).encode()
    calls = _mock_provider(
        monkeypatch, bcb_sgs, primary_host="api.bcb.gov.br",
        candidate_host="api.bcb.gov.br", candidate_body=candidate_body,
    )
    result = bcb_sgs.fetch_sgs_window(
        code=433, start_date="2026-01-01", end_date="2026-01-10", use_cache=False,
    )
    operation = store.latest_source_extractions(1)[0]
    receipt = store.source_acquisition_commit(str(operation["extraction_id"]))
    assert len(calls) == 2 and len(result) == 10
    assert receipt is not None and receipt["selected_route"] == "candidate"
    assert len(store._query(
        "SELECT * FROM source_observation_origins WHERE extraction_id = ?",
        (operation["extraction_id"],),
    )) == 10


def test_postgres_real_sidra_producer_selects_and_persists_v3_dataset(
    postgres_store: object, monkeypatch: pytest.MonkeyPatch
) -> None:
    import candidate_acquisition
    from datafetchers import ibge_sidra

    store = postgres_store
    service = _fixture_service("ibge_sidra:v3:pim_industria", "2026-10-01")
    monkeypatch.setattr(candidate_acquisition, "DEFAULT_CANDIDATE_ACQUISITION", service)
    spec = ibge_sidra.SIDRA_DATASETS["pim_industria"]
    periods = {
        date(2026, month, 1).strftime("%Y%m"): str(month + 100)
        for month in range(1, 11)
    }
    payload = [{
        "id": str(spec.variable_id),
        "variavel": spec.variable_label,
        "unidade": spec.unit_label,
        "resultados": [{
            "classificacoes": [{
                "id": str(spec.classification_id),
                "categoria": {str(spec.category_id): spec.category_label},
            }],
            "series": [{
                "localidade": {
                    "id": "1", "nome": "Brasil",
                    "nivel": {"id": "N1", "nome": "Brasil"},
                },
                "serie": periods,
            }],
        }],
    }]
    monkeypatch.setattr(
        ibge_sidra, "SIDRA_DATASETS", MappingProxyType({spec.key: spec})
    )
    calls = _mock_provider(
        monkeypatch, ibge_sidra, primary_host="apisidra.ibge.gov.br",
        candidate_host="servicodados.ibge.gov.br",
        candidate_body=json.dumps(payload).encode(),
    )
    result = ibge_sidra.refresh_sidra_data()
    operation = store.latest_source_extractions(1)[0]
    receipt = store.source_acquisition_commit(str(operation["extraction_id"]))
    assert len(calls) == 2 and len(result[spec.key]) == 10
    assert receipt is not None and receipt["selected_route"] == "candidate"
    assert len(store._query(
        "SELECT * FROM source_observation_origins WHERE extraction_id = ?",
        (operation["extraction_id"],),
    )) == 10
