from __future__ import annotations

import json
import sqlite3
from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
from pathlib import Path
from types import MappingProxyType
from typing import ClassVar

import pytest
from fastapi.testclient import TestClient

from candidate_eligibility import (
    CandidateApproval,
    parser_semantic_sha256,
    profile_semantic_sha256,
)
from db import store
from main import app
from source_candidates import CandidateStatus, get_candidate_profile


@pytest.fixture()
def isolated_database(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    if store._conn is not None:
        store._conn.close()
    store._conn = None
    monkeypatch.setattr(store, "DB_PATH", tmp_path / "canary-executor.db")
    monkeypatch.setattr(store, "DATABASE_URL", "")
    store.init_db()
    yield
    if store._conn is not None:
        store._conn.close()
    store._conn = None


def _qualified(candidate_id: str):
    profile = replace(
        get_candidate_profile(candidate_id),
        status=CandidateStatus.ELIGIBLE,
        enabled_for_ingestion=True,
        code_reviewed=True,
        approved_proof_sha256="d" * 64,
        license_status="verified",
    )
    approval = CandidateApproval(
        candidate_id,
        profile.source_contract_version,
        profile_semantic_sha256(profile),
        parser_semantic_sha256(candidate_id),
        "d" * 64,
        10,
        "2026-01-10",
        True,
        True,
        True,
    )
    return profile, approval


def _sidra_payloads():
    from datafetchers.ibge_sidra import SIDRA_DATASETS

    spec = SIDRA_DATASETS["pim_industria"]
    primary = [
        {"NC": "1", "NN": "Brasil", "MC": "Valor", "MN": "Variável", "V": "Valor",
         "D1C": "Brasil", "D1N": "Brasil", "D2C": "Variável", "D2N": "Variável",
         "D3C": "Mês", "D3N": "Mês", "D4C": "Categoria", "D4N": "Categoria"},
        {"NC": "1", "NN": "Brasil", "MC": "30", "MN": spec.unit_label, "V": "12,5",
         "D1C": "1", "D1N": "Brasil", "D2C": str(spec.variable_id),
         "D2N": spec.variable_label, "D3C": "202512", "D3N": "dezembro 2025",
         "D4C": str(spec.category_id), "D4N": spec.category_label},
    ]
    candidate = [{
        "id": str(spec.variable_id),
        "variavel": spec.variable_label,
        "unidade": spec.unit_label,
        "resultados": [{
            "classificacoes": [{
                "id": str(spec.classification_id),
                "categoria": {str(spec.category_id): spec.category_label},
            }],
            "series": [{
                "localidade": {"id": "1", "nome": "Brasil", "nivel": {"id": "N1", "nome": "Brasil"}},
                "serie": {"202512": "12,5"},
            }],
        }],
    }]
    return primary, candidate


class _Response:
    status_code = 200
    headers: ClassVar[dict[str, str]] = {"content-type": "application/json"}

    def __init__(self, payload: object, url: str):
        self.content = json.dumps(payload).encode()
        self.url = url

    def raise_for_status(self) -> None:
        return None


def _configure_route(monkeypatch: pytest.MonkeyPatch, profile, approval) -> None:
    from routes import source_candidates

    monkeypatch.setenv("XBRAY_ENV", "local")
    monkeypatch.setenv("SOURCE_FALLBACK_MODE", "canary")
    monkeypatch.setattr("main.init_db", lambda: None)
    monkeypatch.setattr(source_candidates.store, "source_access_states", dict)
    monkeypatch.setattr(
        source_candidates.store,
        "source_candidate_activity",
        lambda _limit: {"extractions": [], "attempts": [], "origins": []},
    )
    monkeypatch.setattr(source_candidates, "get_candidate_profile", lambda _candidate_id: profile)
    monkeypatch.setattr(
        source_candidates,
        "APPROVED_CANDIDATE_APPROVALS",
        MappingProxyType({profile.candidate_id: approval}),
    )
    monkeypatch.setattr(
        source_candidates,
        "readiness_snapshot",
        lambda **_kwargs: {"candidates": [{"candidate_id": profile.candidate_id, "ready": True}]},
    )


def _request(candidate_id: str, operation_id: str, *, as_of: str = "2026-10-20"):
    from source_candidates import get_candidate_profile as get_profile

    return {
        "candidate_id": candidate_id,
        "as_of": as_of,
        "dry_run": False,
        "confirm_write": True,
        "expected_proof_sha256": "d" * 64,
        "expected_contract_version": get_profile(candidate_id).source_contract_version,
        "operation_id": operation_id,
    }


@pytest.mark.parametrize("candidate_id", ["bcb_sgs:csv:433", "ibge_sidra:v3:pim_industria"])
def test_route_pairs_raw_providers_and_atomically_persists_then_replays(
    candidate_id: str,
    isolated_database,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    profile, approval = _qualified(candidate_id)
    _configure_route(monkeypatch, profile, approval)
    if profile.source.value == "bcb_sgs":
        primary = [{"data": f"{day:02d}/10/2026", "valor": str(day)} for day in range(11, 21)]
        alternate = "data;valor\n" + "\n".join(f"{day:02d}/10/2026;{day}" for day in range(11, 21))
        payloads: list[object] = [primary, alternate]
    else:
        payloads = list(_sidra_payloads())
    requests: list[tuple[str, object]] = []

    def raw_get(url: str, **kwargs):
        from ingestion_policy import current_retry_budget

        current_retry_budget().reserve_request()
        if profile.source.value == "ibge_sidra" and not requests:
            current_retry_budget().reserve_request()  # mocked redirect hop
        requests.append((url, kwargs.get("params")))
        payload = payloads[len(requests) - 1]
        return _Response(payload, url) if isinstance(payload, list) else _CsvResponse(payload, url)

    monkeypatch.setattr("provider_http.get_provider_response", raw_get)
    headers_before = store._query("SELECT key, value FROM meta ORDER BY key")
    with TestClient(app) as client:
        first = client.post("/api/source-candidates/canary", json=_request(candidate_id, "route-op-1"))
        assert first.status_code == 200, first.text
        body = first.json()
        assert body["committed"] is True
        expected_requests = 3 if profile.source.value == "ibge_sidra" else 2
        assert body["request_count"] == expected_requests
        assert body["selected_route"] == "candidate"
        assert body["business_writes"] == 10 if profile.source.value == "bcb_sgs" else 1

        second = client.post("/api/source-candidates/canary", json=_request(candidate_id, "route-op-1"))
        assert second.status_code == 200
        assert second.json()["status"] == "replayed_known_commit"
        assert second.json()["reference_period"] == body["reference_period"]
        assert len(requests) == 2

        conflict = client.post(
            "/api/source-candidates/canary",
            json=_request(candidate_id, "route-op-1", as_of="2026-10-19"),
        )
        assert conflict.status_code == 409

    assert store.source_canary_binding("route-op-1") is not None
    assert store.source_canary_binding("route-op-1")["request_count"] == expected_requests
    assert store.source_acquisition_commit("route-op-1")["observation_count"] == body["selected_observations"]
    assert len(store._query("SELECT * FROM source_observation_origins WHERE extraction_id = ?", ("route-op-1",))) == body["origin_count"]
    assert store._query("SELECT key, value FROM meta ORDER BY key") == headers_before
    if profile.source.value == "bcb_sgs":
        assert len(store._query("SELECT * FROM sgs WHERE series_code = 433")) == 10
        assert len(store._query("SELECT * FROM observation_vintages WHERE series_key = 'ipca'")) == 10
    else:
        assert len(store._query("SELECT * FROM sidra_observations WHERE dataset_key = 'pim_industria'")) == 1


class _CsvResponse(_Response):
    headers: ClassVar[dict[str, str]] = {"content-type": "text/csv"}

    def __init__(self, payload: str, url: str):
        self.content = payload.encode()
        self.url = url


@pytest.mark.parametrize("failure", ["primary", "mismatch", "quota"])
def test_failed_or_disagreeing_pair_writes_nothing(
    failure: str,
    isolated_database,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    profile, approval = _qualified("bcb_sgs:csv:433")
    _configure_route(monkeypatch, profile, approval)
    valid = [{"data": f"{day:02d}/10/2026", "valor": str(day)} for day in range(11, 21)]
    mismatch = [{"data": f"{day:02d}/10/2026", "valor": str(day + (1 if day == 20 else 0))} for day in range(11, 21)]
    csv_rows = mismatch if failure == "mismatch" else valid
    csv_text = "data;valor\n" + "\n".join(f"{row['data']};{row['valor']}" for row in csv_rows)
    requests: list[str] = []

    def raw_get(url: str, **_kwargs):
        from ingestion_policy import current_retry_budget

        requests.append(url)
        if failure == "primary":
            current_retry_budget().reserve_request()
            raise RuntimeError("mock primary unavailable")
        if failure == "quota":
            from ingestion_policy import RetryDeferred

            raise RetryDeferred("quota", retry_not_before="2026-01-11T00:00:00+00:00")
        current_retry_budget().reserve_request()
        return _Response(valid, url) if len(requests) == 1 else _CsvResponse(csv_text, url)

    monkeypatch.setattr("provider_http.get_provider_response", raw_get)
    with TestClient(app) as client:
        response = client.post("/api/source-candidates/canary", json=_request(profile.candidate_id, f"failed-{failure}"))
    assert response.status_code in {409, 429, 502}
    assert store._query("SELECT * FROM sgs WHERE series_code = 433") == []
    assert store._query("SELECT * FROM observation_vintages") == []
    assert store._query("SELECT * FROM source_observation_origins") == []
    assert store.source_acquisition_commit(f"failed-{failure}") is None
    assert store.source_canary_binding(f"failed-{failure}") is None
    assert len(requests) == (1 if failure == "primary" else 1 if failure == "quota" else 2)


@pytest.mark.parametrize("candidate_id", ["bcb_sgs:csv:433", "ibge_sidra:v3:pim_industria"])
def test_origin_trigger_rolls_back_every_canary_write(candidate_id, isolated_database, monkeypatch):
    profile, approval = _qualified(candidate_id)
    _configure_route(monkeypatch, profile, approval)
    responses = (
        [[{"data": f"{day:02d}/10/2026", "valor": str(day)} for day in range(11, 21)],
         "data;valor\n" + "\n".join(f"{day:02d}/10/2026;{day}" for day in range(11, 21))]
        if profile.source.value == "bcb_sgs"
        else list(_sidra_payloads())
    )
    calls = 0

    def raw_get(url: str, **kwargs):
        nonlocal calls
        from ingestion_policy import current_retry_budget

        current_retry_budget().reserve_request()
        payload = responses[calls]
        calls += 1
        return _Response(payload, url) if isinstance(payload, list) else _CsvResponse(payload, url)

    monkeypatch.setattr("provider_http.get_provider_response", raw_get)
    store._execute(
        "CREATE TRIGGER reject_canary_origin BEFORE INSERT ON source_observation_origins "
        "BEGIN SELECT RAISE(ABORT, 'injected origin failure'); END"
    )
    with TestClient(app) as client:
        response = client.post("/api/source-candidates/canary", json=_request(candidate_id, "rollback-op"))
    assert response.status_code == 502
    assert store._query("SELECT * FROM source_observation_origins") == []
    assert store.source_acquisition_commit("rollback-op") is None
    assert store.source_canary_binding("rollback-op") is None
    assert store._query("SELECT * FROM sgs WHERE series_code = 433") == []
    assert store._query("SELECT * FROM observation_vintages") == []
    assert store._query("SELECT * FROM sidra_observations") == []


def test_default_approval_and_authentication_guards_make_zero_provider_requests(
    isolated_database, monkeypatch: pytest.MonkeyPatch
) -> None:
    profile = get_candidate_profile("bcb_sgs:csv:433")
    calls: list[str] = []
    monkeypatch.setenv("XBRAY_ENV", "production")
    monkeypatch.setenv("XBRAY_REFRESH_TOKEN", "test-token")
    monkeypatch.setenv("SOURCE_FALLBACK_MODE", "canary")
    monkeypatch.setattr("main.init_db", lambda: None)
    monkeypatch.setattr("provider_http.get_provider_response", lambda url, **_kwargs: calls.append(url))
    monkeypatch.setattr("routes.source_candidates.APPROVED_CANDIDATE_APPROVALS", {})
    with TestClient(app) as client:
        unauthenticated = client.post(
            "/api/source-candidates/canary",
            json={"candidate_id": profile.candidate_id, "dry_run": False, "confirm_write": True},
        )
        unapproved = client.post(
            "/api/source-candidates/canary",
            headers={"Authorization": "Bearer test-token"},
            json={
                **_request(profile.candidate_id, "unapproved"),
                "expected_proof_sha256": "a" * 64,
            },
        )
    assert unauthenticated.status_code == 401
    assert unapproved.status_code == 409
    assert calls == []
    assert store._query("SELECT * FROM sgs WHERE series_code = 433") == []
    assert store._query("SELECT * FROM source_canary_bindings") == []


@pytest.mark.parametrize("failure", ["commit_response", "logging"])
def test_uncertain_commit_and_postcommit_logging_failure_reconcile_receipt(
    failure: str, isolated_database, monkeypatch: pytest.MonkeyPatch
) -> None:
    profile, approval = _qualified("bcb_sgs:csv:433")
    _configure_route(monkeypatch, profile, approval)
    primary = [{"data": f"{day:02d}/10/2026", "valor": str(day)} for day in range(11, 21)]
    alternate = "data;valor\n" + "\n".join(f"{day:02d}/10/2026;{day}" for day in range(11, 21))
    responses: list[object] = [primary, alternate]
    calls = 0

    def raw_get(url: str, **kwargs):
        nonlocal calls
        from ingestion_policy import current_retry_budget

        current_retry_budget().reserve_request()
        payload = responses[calls]
        calls += 1
        return _Response(payload, url) if isinstance(payload, list) else _CsvResponse(payload, url)

    monkeypatch.setattr("provider_http.get_provider_response", raw_get)
    if failure == "commit_response":
        original = store.upsert_sgs

        def commit_then_disconnect(*args, **kwargs):
            original(*args, **kwargs)
            raise sqlite3.OperationalError("injected lost commit response")

        monkeypatch.setattr(store, "upsert_sgs", commit_then_disconnect)
    else:
        original_finish = store.finish_source_extraction

        def commit_log_then_error(row):
            original_finish(row)
            raise sqlite3.OperationalError("injected post-commit logging error")

        monkeypatch.setattr(store, "finish_source_extraction", commit_log_then_error)
    with TestClient(app) as client:
        response = client.post("/api/source-candidates/canary", json=_request(profile.candidate_id, f"reconcile-{failure}"))
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["committed"] is True
    assert body["business_writes"] == 10
    assert body["origin_count"] == 10
    assert body["request_count"] == 2
    assert body["status"] == (
        "reconciled_known_commit" if failure == "commit_response" else "committed_with_reporting_error"
    )
    assert store.source_canary_binding(f"reconcile-{failure}") is not None
    assert store.source_acquisition_commit(f"reconcile-{failure}") is not None
    assert len(store._query("SELECT * FROM sgs WHERE series_code = 433")) == 10
    assert calls == 2


@pytest.mark.parametrize(
    ("period", "frequency", "expected"),
    [
        ("2025", "annual", "2025-01-01"),
        ("202512", "monthly", "2025-12-01"),
        ("20254", "quarterly", "2025-10-01"),
    ],
)
def test_sidra_reference_period_maps_to_valid_iso_day(period, frequency, expected):
    from canary_executor import _sidra_reference_date

    assert _sidra_reference_date(period, frequency) == expected


def test_unchanged_sgs_appends_origin_without_fake_vintage_and_revision_is_quarantined(
    isolated_database, monkeypatch: pytest.MonkeyPatch
) -> None:
    profile, approval = _qualified("bcb_sgs:csv:433")
    _configure_route(monkeypatch, profile, approval)
    records = [{"data": f"{day:02d}/10/2026", "valor": str(day)} for day in range(11, 21)]
    store.upsert_sgs(433, records)
    prior_vintages = len(store._query("SELECT * FROM observation_vintages WHERE series_key = 'ipca'"))
    payloads: list[object] = [records, "data;valor\n" + "\n".join(f"{row['data']};{row['valor']}" for row in records)]
    calls = 0

    def raw_get(url: str, **kwargs):
        nonlocal calls
        from ingestion_policy import current_retry_budget

        current_retry_budget().reserve_request()
        payload = payloads[calls]
        calls += 1
        return _Response(payload, url) if isinstance(payload, list) else _CsvResponse(payload, url)

    monkeypatch.setattr("provider_http.get_provider_response", raw_get)
    with TestClient(app) as client:
        same = client.post("/api/source-candidates/canary", json=_request(profile.candidate_id, "unchanged-op"))
    assert same.status_code == 200, same.text
    assert same.json()["business_writes"] == 0
    assert len(store._query("SELECT * FROM observation_vintages WHERE series_key = 'ipca'")) == prior_vintages
    assert len(store._query("SELECT * FROM source_observation_origins")) == 10

    revised = [{**row, "valor": "999" if row["data"] == "11/10/2026" else row["valor"]} for row in records]
    new_payloads: list[object] = [revised, "data;valor\n" + "\n".join(f"{row['data']};{row['valor']}" for row in revised)]
    calls = 0
    payloads[:] = new_payloads
    with TestClient(app) as client:
        rejected = client.post("/api/source-candidates/canary", json=_request(profile.candidate_id, "revision-op"))
    assert rejected.status_code == 409
    assert store.query_sgs(433, "2026-10-11", "2026-10-11")[0]["valor"] == "11.0"
    assert store.source_canary_binding("revision-op") is None
    assert len(store._query("SELECT * FROM source_observation_origins")) == 10


def test_same_operation_concurrent_requests_are_serialized_before_http(
    isolated_database, monkeypatch: pytest.MonkeyPatch
) -> None:
    profile, approval = _qualified("bcb_sgs:csv:433")
    _configure_route(monkeypatch, profile, approval)
    from threading import Barrier

    gate = Barrier(2)
    primary = [{"data": f"{day:02d}/10/2026", "valor": str(day)} for day in range(11, 21)]
    alternate = "data;valor\n" + "\n".join(f"{day:02d}/10/2026;{day}" for day in range(11, 21))
    payloads: list[object] = [primary, alternate]
    calls = 0

    def raw_get(url: str, **kwargs):
        nonlocal calls
        from ingestion_policy import current_retry_budget

        current_retry_budget().reserve_request()
        calls += 1
        payload = payloads[calls - 1]
        return _Response(payload, url) if isinstance(payload, list) else _CsvResponse(payload, url)

    monkeypatch.setattr("provider_http.get_provider_response", raw_get)
    def post():
        gate.wait(timeout=5)
        with TestClient(app) as client:
            return client.post("/api/source-candidates/canary", json=_request(profile.candidate_id, "concurrent-op"))

    with ThreadPoolExecutor(max_workers=2) as executor:
        responses = [future.result(timeout=15) for future in [executor.submit(post), executor.submit(post)]]
    assert [response.status_code for response in responses] == [200, 200]
    assert calls == 2
    assert len(store._query("SELECT * FROM source_canary_bindings WHERE operation_id = 'concurrent-op'")) == 1
    assert len(store._query("SELECT * FROM source_observation_origins WHERE extraction_id = 'concurrent-op'")) == 10


def test_successful_canary_does_not_clear_preexisting_primary_breaker(
    isolated_database, monkeypatch: pytest.MonkeyPatch
) -> None:
    profile, approval = _qualified("bcb_sgs:csv:433")
    _configure_route(monkeypatch, profile, approval)
    store._execute(
        "INSERT INTO source_access_state "
        "(scope_id, consecutive_temporary_failures, state, opened_until, retry_not_before, updated_at) "
        "VALUES (?, 3, 'open', ?, ?, ?)",
        ("bcb_sgs:primary:primary", "2026-10-27T00:00:00+00:00", "2026-10-27T00:00:00+00:00", "2026-10-26T00:00:00+00:00"),
    )
    primary = [{"data": f"{day:02d}/10/2026", "valor": str(day)} for day in range(11, 21)]
    alternate = "data;valor\n" + "\n".join(f"{day:02d}/10/2026;{day}" for day in range(11, 21))
    payloads: list[object] = [primary, alternate]
    calls = 0

    def raw_get(url: str, **_kwargs):
        nonlocal calls
        from ingestion_policy import current_retry_budget

        current_retry_budget().reserve_request()
        payload = payloads[calls]
        calls += 1
        return _Response(payload, url) if isinstance(payload, list) else _CsvResponse(payload, url)

    monkeypatch.setattr("provider_http.get_provider_response", raw_get)
    with TestClient(app) as client:
        response = client.post("/api/source-candidates/canary", json=_request(profile.candidate_id, "breaker-op"))
    assert response.status_code == 200, response.text
    state = store._query(
        "SELECT state, consecutive_temporary_failures, retry_not_before "
        "FROM source_access_state WHERE scope_id = ?",
        ("bcb_sgs:primary:primary",),
    )[0]
    assert state == {
        "state": "open",
        "consecutive_temporary_failures": 3,
        "retry_not_before": "2026-10-27T00:00:00+00:00",
    }


@pytest.mark.parametrize("existing_value", [12.5, 11.0])
def test_sidra_equal_value_adds_only_origin_and_unordered_revision_is_rejected(
    existing_value: float, isolated_database, monkeypatch: pytest.MonkeyPatch
) -> None:
    from datafetchers.ibge_sidra import SIDRA_DATASETS
    from db.sidra_store import insert_sidra_observations, query_sidra_history

    profile, approval = _qualified("ibge_sidra:v3:pim_industria")
    _configure_route(monkeypatch, profile, approval)
    spec = SIDRA_DATASETS["pim_industria"]
    insert_sidra_observations([{
        "dataset_key": spec.key,
        "table_id": spec.table_id,
        "variable_id": spec.variable_id,
        "classification_id": spec.classification_id,
        "category_id": spec.category_id,
        "territory_level": spec.territory_level,
        "territory_code": spec.territory_code,
        "reference_period": "202512",
        "reference_label": "dezembro 2025",
        "value": existing_value,
        "unit": spec.unit,
        "base_version": spec.base_version,
        "source_published_at": None,
    }])
    payloads = list(_sidra_payloads())
    calls = 0

    def raw_get(url: str, **kwargs):
        nonlocal calls
        from ingestion_policy import current_retry_budget

        current_retry_budget().reserve_request()
        payload = payloads[calls]
        calls += 1
        return _Response(payload, url)

    monkeypatch.setattr("provider_http.get_provider_response", raw_get)
    operation_id = "sidra-equal" if existing_value == 12.5 else "sidra-revision"
    with TestClient(app) as client:
        response = client.post(
            "/api/source-candidates/canary",
            json=_request(profile.candidate_id, operation_id),
        )
    if existing_value == 12.5:
        assert response.status_code == 200, response.text
        assert response.json()["business_writes"] == 0
        assert len(query_sidra_history(spec.key)) == 1
        assert len(store._query("SELECT * FROM source_observation_origins")) == 1
        assert store.source_canary_binding(operation_id) is not None
    else:
        assert response.status_code == 409
        assert len(query_sidra_history(spec.key)) == 1
        assert query_sidra_history(spec.key)[0]["value"] == 11.0
        assert store._query("SELECT * FROM source_observation_origins") == []
        assert store.source_canary_binding(operation_id) is None
