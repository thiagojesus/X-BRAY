import json
from collections.abc import Mapping
from dataclasses import replace
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from types import MappingProxyType
from typing import Any

import httpx
import pytest

from candidate_acquisition import CandidateAcquisitionService
from candidate_eligibility import (
    CandidateApproval,
    parser_semantic_sha256,
    profile_semantic_sha256,
)
from datafetchers.bcb_sgs import InvalidSGSPayloadError
from ingestion_policy import RetryDeferred
from source_candidates import CandidateStatus, get_candidate_profile
from source_ingestion import SourceOperationError


@pytest.fixture()
def isolated_store(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    from db import store

    if store._conn is not None:
        store._conn.close()
    store._conn = None
    monkeypatch.setattr(store, "DB_PATH", tmp_path / "producer-integration.db")
    monkeypatch.setattr(store, "DATABASE_URL", "")
    store.init_db()
    yield store
    if store._conn is not None:
        store._conn.close()
    store._conn = None


def _approved_service(
    candidate_id: str, contract_id: str, latest_reference: str
) -> CandidateAcquisitionService:
    catalog_profile = get_candidate_profile(candidate_id)
    proof_sha = "d" * 64
    profile = replace(
        catalog_profile,
        status=CandidateStatus.ELIGIBLE,
        enabled_for_ingestion=True,
        code_reviewed=True,
        approved_proof_sha256=proof_sha,
        license_status="verified",
    )
    approval = CandidateApproval(
        candidate_id=candidate_id,
        contract_version=profile.source_contract_version,
        profile_semantic_sha256=profile_semantic_sha256(profile),
        parser_semantic_sha256=parser_semantic_sha256(candidate_id),
        proof_sha256=proof_sha,
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


def _mock_http(
    monkeypatch: pytest.MonkeyPatch,
    module: Any,
    primary_host: str,
    candidate_host: str,
    candidate_body: bytes,
    *,
    primary_status: int = 502,
    primary_body: bytes = b"temporary failure",
) -> list[str]:
    from provider_http import get_provider_response as real_get_provider_response

    calls: list[str] = []

    def request(
        url: str,
        *,
        timeout: float,
        follow_redirects: bool,
        headers: Mapping[str, str] | None,
        params: Mapping[str, str] | None,
    ) -> httpx.Response:
        del timeout, follow_redirects, headers
        calls.append(url)
        primary_request = primary_host in url and (
            primary_host != candidate_host or (params or {}).get("formato") == "json"
        )
        status = primary_status if primary_request else 200
        body = primary_body if primary_request else candidate_body
        return httpx.Response(
            status,
            content=body,
            request=httpx.Request("GET", url),
        )

    def bounded_get(url: str, **kwargs: object):
        return real_get_provider_response(url, request=request, **kwargs)

    monkeypatch.setattr(module, "get_provider_response", bounded_get)
    return calls


def test_sgs_refresh_entrypoint_selects_and_commits_candidate_atomically(
    isolated_store, monkeypatch: pytest.MonkeyPatch
):
    import candidate_acquisition
    from datafetchers import bcb_sgs
    from db import store

    profile_id = "bcb_sgs:csv:433"
    service = _approved_service(profile_id, "sgs:433", "2026-01-10")
    monkeypatch.setattr(candidate_acquisition, "DEFAULT_CANDIDATE_ACQUISITION", service)
    start = date(2026, 1, 1)
    rows = [
        f"{(start + timedelta(days=offset)):%d/%m/%Y};{offset + 1},25"
        for offset in range(10)
    ]
    calls = _mock_http(
        monkeypatch,
        bcb_sgs,
        "api.bcb.gov.br",
        "api.bcb.gov.br",
        ("data;valor\n" + "\n".join(rows) + "\n").encode(),
    )

    result = bcb_sgs.fetch_sgs_window(
        code=433,
        start_date="2026-01-01",
        end_date="2026-01-10",
        use_cache=False,
    )

    assert len(calls) == 2
    assert result[0]["data"] == "01/01/2026"
    assert len(isolated_store._query("SELECT * FROM sgs WHERE series_code = 433")) == 10
    assert len(isolated_store._query("SELECT * FROM source_observation_origins")) == 10
    operation = isolated_store._query("SELECT * FROM source_extractions")
    receipt = store.source_acquisition_commit(operation[0]["extraction_id"])
    assert receipt is not None and receipt["selected_route"] == "candidate"
    assert len(operation) == 1 and operation[0]["status"] == "success"
    attempts = isolated_store._query(
        "SELECT route, status, request_count, retry_count FROM source_acquisition_attempts "
        "ORDER BY started_at, attempt_id"
    )
    assert [item["route"] for item in attempts] == ["primary", "candidate"]
    assert [item["request_count"] for item in attempts] == [1, 1]
    assert [item["retry_count"] for item in attempts] == [0, 0]
    assert operation[0]["request_count"] == 2
    assert operation[0]["retry_count"] == 0


def test_sidra_refresh_entrypoint_selects_v3_and_commits_full_dataset(
    isolated_store, monkeypatch: pytest.MonkeyPatch
):
    import candidate_acquisition
    from datafetchers import ibge_sidra
    from db import store

    profile_id = "ibge_sidra:v3:pim_industria"
    service = _approved_service(profile_id, "sidra:pim_industria", "2026-10-01")
    monkeypatch.setattr(candidate_acquisition, "DEFAULT_CANDIDATE_ACQUISITION", service)
    spec = ibge_sidra.SIDRA_DATASETS["pim_industria"]
    periods: dict[str, str] = {}
    for index in range(10):
        current = date(2026, 1 + index, 1)
        periods[current.strftime("%Y%m")] = str(index + 100)
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
    monkeypatch.setattr(ibge_sidra, "SIDRA_DATASETS", MappingProxyType({spec.key: spec}))
    calls = _mock_http(
        monkeypatch,
        ibge_sidra,
        "apisidra.ibge.gov.br",
        "servicodados.ibge.gov.br",
        json.dumps(payload).encode(),
    )

    refreshed = ibge_sidra.refresh_sidra_data()

    assert len(calls) == 2
    assert len(refreshed[spec.key]) == 10
    assert len(isolated_store._query("SELECT * FROM sidra_observations")) == 10
    assert len(isolated_store._query("SELECT * FROM source_observation_origins")) == 10
    operation = isolated_store._query("SELECT * FROM source_extractions")
    print(operation)
    assert len(operation) == 1
    receipt = store.source_acquisition_commit(operation[0]["extraction_id"])
    assert receipt is not None and receipt["selected_route"] == "candidate"
    attempts = isolated_store._query(
        "SELECT route, status, request_count, retry_count FROM source_acquisition_attempts "
        "ORDER BY started_at, attempt_id"
    )
    assert [item["route"] for item in attempts] == ["primary", "candidate"]
    assert [item["request_count"] for item in attempts] == [1, 1]
    assert [item["retry_count"] for item in attempts] == [0, 0]
    assert operation[0]["request_count"] == 2
    assert operation[0]["retry_count"] == 0


@pytest.mark.parametrize("failure_status", [429, 200])
def test_sgs_unsafe_primary_failure_never_calls_candidate(
    isolated_store, monkeypatch: pytest.MonkeyPatch, failure_status: int
):
    import candidate_acquisition
    from datafetchers import bcb_sgs

    service = _approved_service("bcb_sgs:csv:433", "sgs:433", "2026-01-10")
    monkeypatch.setattr(candidate_acquisition, "DEFAULT_CANDIDATE_ACQUISITION", service)
    calls = _mock_http(
        monkeypatch,
        bcb_sgs,
        "api.bcb.gov.br",
        "api.bcb.gov.br",
        b"data;valor\n01/01/2026;1,0\n",
        primary_status=failure_status,
        primary_body=b'{"unexpected": []}' if failure_status == 200 else b"quota",
    )

    expected_error = RetryDeferred if failure_status == 429 else InvalidSGSPayloadError
    with pytest.raises(expected_error) as raised:
        bcb_sgs.fetch_sgs_window(
            code=433,
            start_date="01/01/2026",
            end_date="10/01/2026",
            use_cache=False,
        )

    assert len(calls) == 1
    if failure_status == 429:
        assert raised.value.error_category.value == "rate_limited"
        assert raised.value.retry_not_before.tzinfo is timezone.utc
    assert isolated_store._query("SELECT * FROM sgs WHERE series_code = 433") == []
    assert isolated_store._query("SELECT * FROM source_acquisition_attempts")[-1]["route"] == "primary"


def test_valid_candidate_persistence_error_rolls_back_and_does_not_retry_other_route(
    isolated_store, monkeypatch: pytest.MonkeyPatch
):
    import candidate_acquisition
    from datafetchers import bcb_sgs

    service = _approved_service("bcb_sgs:csv:433", "sgs:433", "2026-01-10")
    monkeypatch.setattr(candidate_acquisition, "DEFAULT_CANDIDATE_ACQUISITION", service)
    start = date(2026, 1, 1)
    candidate_body = (
        "data;valor\n"
        + "\n".join(
            f"{(start + timedelta(days=offset)):%d/%m/%Y};{offset + 1},25"
            for offset in range(10)
        )
        + "\n"
    ).encode()
    calls = _mock_http(
        monkeypatch,
        bcb_sgs,
        "api.bcb.gov.br",
        "api.bcb.gov.br",
        candidate_body,
    )
    isolated_store._execute(
        "CREATE TRIGGER reject_origin BEFORE INSERT ON source_observation_origins "
        "BEGIN SELECT RAISE(ABORT, 'injected origin failure'); END"
    )

    with pytest.raises(SourceOperationError):
        bcb_sgs.fetch_sgs_window(
            code=433,
            start_date="01/01/2026",
            end_date="10/01/2026",
            use_cache=False,
        )

    assert len(calls) == 2
    assert isolated_store._query("SELECT * FROM sgs WHERE series_code = 433") == []
    assert isolated_store._query("SELECT * FROM observation_vintages WHERE provider = 'bcb_sgs'") == []
    assert isolated_store._query("SELECT * FROM source_observation_origins") == []
    assert isolated_store._query("SELECT * FROM source_acquisition_commits") == []
    attempts = isolated_store._query(
        "SELECT route, status FROM source_acquisition_attempts ORDER BY started_at, attempt_id"
    )
    assert [row["route"] for row in attempts] == ["primary", "candidate"]
    assert attempts[-1]["status"] == "persistence_error"


def test_current_unqualified_catalog_keeps_sgs_on_primary_only(
    isolated_store, monkeypatch: pytest.MonkeyPatch
):
    import candidate_acquisition
    from datafetchers import bcb_sgs

    assert candidate_acquisition.DEFAULT_CANDIDATE_ACQUISITION.resolve(
        "bcb_sgs:csv:433", "sgs:433"
    ) == (None, None, "candidate_acquisition_globally_disabled")
    assert not candidate_acquisition.APPROVED_CANDIDATE_APPROVALS
    assert all(
        profile.status is not CandidateStatus.ELIGIBLE
        and not profile.enabled_for_ingestion
        and profile.license_status != "verified"
        for profile in candidate_acquisition.CANDIDATE_REGISTRY.values()
    )
    calls: list[tuple[str, dict[str, object]]] = []

    def fail_primary(url: str, **kwargs: object) -> None:
        calls.append((url, kwargs))
        raise TimeoutError("fixture primary timeout")

    monkeypatch.setattr(bcb_sgs, "get_provider_response_bounded", fail_primary)
    with pytest.raises(TimeoutError):
        bcb_sgs.fetch_sgs_window(
            code=433,
            start_date="01/01/2026",
            end_date="10/01/2026",
            use_cache=False,
        )

    assert calls
    assert all("formato=json" in url for url, _kwargs in calls)
    assert isolated_store._query("SELECT * FROM sgs WHERE series_code = 433") == []


def test_primary_sgs_success_is_selected_without_alternative_request(
    isolated_store, monkeypatch: pytest.MonkeyPatch
):
    import candidate_acquisition
    from datafetchers import bcb_sgs

    monkeypatch.setattr(
        candidate_acquisition,
        "DEFAULT_CANDIDATE_ACQUISITION",
        _approved_service("bcb_sgs:csv:433", "sgs:433", "2026-01-10"),
    )
    start = date(2026, 1, 1)
    primary_records = [
        {"data": (start + timedelta(days=i)).strftime("%d/%m/%Y"), "valor": str(i + 1)}
        for i in range(10)
    ]
    candidate_csv = b"data;valor\n" + b"".join(
        f"{(start + timedelta(days=i)):%d/%m/%Y};{i + 1},25\n".encode()
        for i in range(10)
    )
    calls = _mock_http(
        monkeypatch,
        bcb_sgs,
        "api.bcb.gov.br",
        "api.bcb.gov.br",
        candidate_csv,
        primary_status=200,
        primary_body=json.dumps(primary_records).encode(),
    )

    result = bcb_sgs.fetch_sgs_window(
        code=433,
        start_date="01/01/2026",
        end_date="10/01/2026",
        use_cache=False,
    )

    assert len(calls) == 1 and len(result) == 10
    receipt = isolated_store._query("SELECT * FROM source_acquisition_commits")
    assert len(receipt) == 1 and receipt[0]["selected_route"] == "primary"
    attempts = isolated_store._query("SELECT route FROM source_acquisition_attempts")
    assert [attempt["route"] for attempt in attempts] == ["primary"]


def test_sgs_commit_response_loss_reconciles_receipt_without_duplicate_write(
    isolated_store, monkeypatch: pytest.MonkeyPatch
):
    import candidate_acquisition
    from datafetchers import bcb_sgs

    service = _approved_service("bcb_sgs:csv:433", "sgs:433", "2026-01-10")
    monkeypatch.setattr(candidate_acquisition, "DEFAULT_CANDIDATE_ACQUISITION", service)
    start = date(2026, 1, 1)
    body = (
        "data;valor\n"
        + "\n".join(
            f"{(start + timedelta(days=offset)):%d/%m/%Y};{offset + 1},25"
            for offset in range(10)
        )
        + "\n"
    ).encode()
    calls = _mock_http(
        monkeypatch, bcb_sgs, "api.bcb.gov.br", "api.bcb.gov.br", body
    )
    original_persist = isolated_store._persist_current_and_vintages

    def commit_then_lose_response(*args: object, **kwargs: object) -> None:
        original_persist(*args, **kwargs)
        raise ConnectionError("injected lost commit response")

    monkeypatch.setattr(
        isolated_store, "_persist_current_and_vintages", commit_then_lose_response
    )

    result = bcb_sgs.fetch_sgs_window(
        code=433,
        start_date="2026-01-01",
        end_date="2026-01-10",
        use_cache=False,
    )

    assert len(calls) == 2
    assert len(result) == 10
    assert len(isolated_store._query("SELECT * FROM sgs WHERE series_code = 433")) == 10
    assert len(isolated_store._query("SELECT * FROM observation_vintages WHERE provider = 'bcb_sgs'")) == 10
    assert len(isolated_store._query("SELECT * FROM source_observation_origins")) == 10
    assert len(isolated_store._query("SELECT * FROM source_acquisition_commits")) == 1
    operation = isolated_store._query("SELECT * FROM source_extractions")
    assert len(operation) == 1 and operation[0]["status"] == "success"


def test_primary_open_transient_breaker_does_not_reset_after_candidate_success(
    isolated_store, monkeypatch: pytest.MonkeyPatch
):
    import candidate_acquisition
    from datafetchers import bcb_sgs

    service = _approved_service("bcb_sgs:csv:433", "sgs:433", "2026-01-10")
    monkeypatch.setattr(candidate_acquisition, "DEFAULT_CANDIDATE_ACQUISITION", service)
    now = datetime.now(timezone.utc)
    isolated_store.record_source_access_failure(
        "bcb_sgs:primary:primary",
        now.isoformat(),
        (now + timedelta(minutes=5)).isoformat(),
        None,
        rate_limited=False,
        failure_count=3,
    )
    start = date(2026, 1, 1)
    body = (
        "data;valor\n"
        + "\n".join(
            f"{(start + timedelta(days=offset)):%d/%m/%Y};{offset + 1},25"
            for offset in range(10)
        )
        + "\n"
    ).encode()
    calls = _mock_http(
        monkeypatch, bcb_sgs, "api.bcb.gov.br", "api.bcb.gov.br", body
    )

    result = bcb_sgs.fetch_sgs_window(
        code=433,
        start_date="2026-01-01",
        end_date="2026-01-10",
        use_cache=False,
    )

    assert len(result) == 10
    assert len(calls) == 1
    assert isolated_store.get_source_access_state("bcb_sgs:primary:primary")["state"] == "open"
    assert isolated_store.get_source_access_state(
        "bcb_sgs:candidate:bcb_sgs:csv:433"
    )["state"] == "closed"


def test_sgs_freshness_failure_rolls_back_candidate_commit_atomically(
    isolated_store, monkeypatch: pytest.MonkeyPatch
):
    import candidate_acquisition
    from datafetchers import bcb_sgs

    monkeypatch.setattr(
        candidate_acquisition,
        "DEFAULT_CANDIDATE_ACQUISITION",
        _approved_service("bcb_sgs:csv:433", "sgs:433", "2026-01-10"),
    )
    start = date(2026, 1, 1)
    body = (
        "data;valor\n" + "\n".join(
            f"{(start + timedelta(days=i)):%d/%m/%Y};{i + 1},25" for i in range(10)
        ) + "\n"
    ).encode()
    calls = _mock_http(
        monkeypatch, bcb_sgs, "api.bcb.gov.br", "api.bcb.gov.br", body
    )

    isolated_store._execute(
        "CREATE TRIGGER fail_sgs_freshness BEFORE INSERT ON meta "
        "WHEN NEW.key LIKE 'sgs_window_success%' "
        "BEGIN SELECT RAISE(ABORT, 'injected freshness metadata failure'); END",
        (),
    )
    with pytest.raises(SourceOperationError):
        bcb_sgs.fetch_sgs_window(
            code=433,
            start_date="2026-01-01",
            end_date="2026-01-10",
            use_cache=False,
        )

    operation = isolated_store._query("SELECT * FROM source_extractions")
    assert len(calls) == 2
    assert isolated_store._query("SELECT * FROM sgs WHERE series_code = 433") == []
    assert isolated_store._query("SELECT * FROM source_observation_origins") == []
    assert isolated_store._query("SELECT * FROM source_acquisition_commits") == []
    assert operation[0]["status"] == "validation_error", operation[0]
    assert operation[0]["persisted_count"] is None


@pytest.mark.parametrize(
    ("status", "body", "expected_exception"),
    [
        (200, b'{"not": "an SGS array"}', InvalidSGSPayloadError),
        (401, b"unauthorized", httpx.HTTPStatusError),
    ],
)
def test_unrecoverable_primary_schema_or_auth_never_calls_candidate(
    isolated_store,
    monkeypatch: pytest.MonkeyPatch,
    status: int,
    body: bytes,
    expected_exception: type[Exception],
):
    import candidate_acquisition
    from datafetchers import bcb_sgs

    monkeypatch.setattr(
        candidate_acquisition,
        "DEFAULT_CANDIDATE_ACQUISITION",
        _approved_service("bcb_sgs:csv:433", "sgs:433", "2026-01-10"),
    )
    calls = _mock_http(
        monkeypatch,
        bcb_sgs,
        "api.bcb.gov.br",
        "api.bcb.gov.br",
        b"data;valor\n01/01/2026;1,0\n",
        primary_status=status,
        primary_body=body,
    )

    with pytest.raises(expected_exception):
        bcb_sgs.fetch_sgs_window(
            code=433,
            start_date="01/01/2026",
            end_date="10/01/2026",
            use_cache=False,
        )

    assert len(calls) == 1
    assert isolated_store._query("SELECT * FROM source_observation_origins") == []
    assert isolated_store._query("SELECT * FROM source_acquisition_commits") == []
    assert [row["route"] for row in isolated_store._query(
        "SELECT route FROM source_acquisition_attempts"
    )] == ["primary"]
