from __future__ import annotations

import json
from datetime import datetime, timezone

import httpx

from source_audit import auditor


def _origin_fixture(source: str = "bcb_sgs"):
    contract_id = "sgs:433" if source == "bcb_sgs" else "sidra:pim_industria"
    candidate_id = "bcb_sgs:csv:433" if source == "bcb_sgs" else "ibge_sidra:v3:pim_industria"
    identity = {"series_code": 433, "reference_date": "2026-10-20"} if source == "bcb_sgs" else {
        "variable_id": 12607, "table_id": 8888, "territory_level": "N1", "territory_code": "1",
        "classification_id": 544, "category_id": 129314, "reference_period": "202610",
        "unit": "Número-índice", "base_version": "2022=100",
    }
    attestation = {
        "contract_version": "candidate-contract-v1", "profile_semantic_sha256": "a" * 64,
        "parser_semantic_sha256": "b" * 64, "proof_sha256": "c" * 64,
    }
    origin = {
        "route": "candidate", "source": source, "candidate_id": candidate_id,
        "extraction_id": "origin-op", "contract_id": contract_id,
        "identity_json": json.dumps(identity), "reference_date": "2026-10-20" if source == "bcb_sgs" else "202610",
        **attestation,
    }
    trusted = {
        **attestation, "contract_id": contract_id,
        "identity_fields": list(identity),
        "provider_code": "433", "unit": "percentual",
        "candidate_url": "https://api.bcb.gov.br/dados/serie/bcdata.sgs.433/dados?formato=csv",
    }
    database = {
        "source_observation_origins": [origin],
        "source_acquisition_commits": [{"extraction_id": "origin-op", "selected_route": "candidate", "observation_count": 1}],
        "candidate_origin_business": ([{"source": source, "series_code": "433", "reference_date": "2026-10-20", "value": 2.0}]
                                      if source == "bcb_sgs" else []),
        "candidate_origin_sidra_business": ([{"source": source, "dataset_key": "pim_industria",
            "reference_date": "202610", "value": 2.0}] if source == "ibge_sidra" else []),
    }
    return candidate_id, trusted, database


def test_unknown_origin_attestation_is_unverified_without_http(monkeypatch):
    _, _, db = _origin_fixture()
    calls = []
    monkeypatch.setattr(auditor, "_get", lambda *args, **kwargs: calls.append(args))
    report = auditor._candidate_origin_audits(db, "2026-10-26T00:00:00+00:00")
    assert report[0]["status"] == "unknown_version"
    assert report[0]["independent_verification"] == "unverified"
    assert report[0]["reason"] == "unknown_or_unapproved_attestation"
    assert calls == []


def test_trusted_origin_compares_raw_official_csv_and_database(monkeypatch):
    candidate_id, trusted, db = _origin_fixture()
    monkeypatch.setitem(auditor.CANDIDATE_ORIGIN_TRUSTED_CONTRACTS, candidate_id, trusted)
    calls = []

    def get(url, params, cap, **kwargs):
        calls.append(url)
        return b'[{"data":"20/10/2026","valor":"2"}]' if params["formato"] == "json" else b"data;valor\n20/10/2026;2\n"

    monkeypatch.setattr(auditor, "_get", get)
    report = auditor._candidate_origin_audits(db, "2026-10-26T00:00:00+00:00")
    assert report[0]["status"] == "matched"
    assert report[0]["primary_source_status"] == "available"
    assert len(calls) == 2
    assert calls[0].startswith("https://api.bcb.gov.br/")
    assert calls[1].startswith("https://api.bcb.gov.br/")


def test_raw_candidate_mismatch_is_not_resolved_by_database_match(monkeypatch):
    candidate_id, trusted, db = _origin_fixture()
    monkeypatch.setitem(auditor.CANDIDATE_ORIGIN_TRUSTED_CONTRACTS, candidate_id, trusted)
    bodies = iter((b'[{"data":"20/10/2026","valor":"2"}]', b"data;valor\n20/10/2026;9\n"))
    monkeypatch.setattr(auditor, "_get", lambda *args, **kwargs: next(bodies))
    result = auditor._candidate_origin_audits(db, "2026-10-26T00:00:00+00:00")[0]
    assert result["status"] == "changed"
    assert {row["cause"] for row in result["mismatches"]} == {"publisher_disagreement", "candidate_business_mismatch"}


def test_primary_429_blocks_candidate_request(monkeypatch):
    candidate_id, trusted, db = _origin_fixture()
    monkeypatch.setitem(auditor.CANDIDATE_ORIGIN_TRUSTED_CONTRACTS, candidate_id, trusted)
    calls = []
    request = httpx.Request("GET", "https://api.bcb.gov.br/")
    response = httpx.Response(429, request=request)

    def get(url, *args, **kwargs):
        calls.append(url)
        raise httpx.HTTPStatusError("rate limited", request=request, response=response)

    monkeypatch.setattr(auditor, "_get", get)
    report = auditor._candidate_origin_audits(db, "2026-10-26T00:00:00+00:00")
    assert report[0]["status"] == "publisher_deferred"
    assert len(calls) == 1


def test_primary_timeout_still_checks_alternate_and_reports_warning(monkeypatch):
    candidate_id, trusted, db = _origin_fixture()
    monkeypatch.setitem(auditor.CANDIDATE_ORIGIN_TRUSTED_CONTRACTS, candidate_id, trusted)
    calls = []

    def get(url, params, cap, **kwargs):
        calls.append(url)
        if params["formato"] == "json":
            raise httpx.ReadTimeout("primary timeout")
        return b"data;valor\n20/10/2026;2\n"

    monkeypatch.setattr(auditor, "_get", get)
    result = auditor._candidate_origin_audits(db, "2026-10-26T00:00:00+00:00")[0]
    assert result["status"] == "matched"
    assert result["primary_source_status"] == "unavailable"
    assert result["warning"] == "primary_source_unavailable_candidate_matches"
    assert len(calls) == 2


def test_sidra_nested_v3_candidate_matches_independent_flattened_primary(monkeypatch):
    candidate_id, trusted, db = _origin_fixture("ibge_sidra")
    trusted.update({
        "dataset_key": "pim_industria", "table_id": "8888", "variable_id": "12607",
        "classification_id": "544", "category_id": "129314", "category_label": "1 Indústria geral",
        "territory_level": "N1", "territory_code": "1", "unit": "Número-índice",
        "unit_code": "30", "identity_unit": "Número-índice", "base_version": "2022=100",
        "primary_url": "https://apisidra.ibge.gov.br/values/t/8888/n1/1/v/12607/p/202610/c544/129314",
        "candidate_url": "https://servicodados.ibge.gov.br/api/v3/agregados/8888/periodos/202610/variaveis/12607",
    })
    monkeypatch.setitem(auditor.CANDIDATE_ORIGIN_TRUSTED_CONTRACTS, candidate_id, trusted)
    primary = [{
        "NC": "8888", "D1C": "1", "D2C": "12607", "D3C": "202610", "D4C": "129314",
        "MC": "30", "V": "2,0",
    }]
    candidate = [{
        "id": "12607", "unidade": "Número-índice", "resultados": [{
            "classificacoes": [{"id": "544", "categoria": {"129314": "1 Indústria geral"}}],
            "series": [{"localidade": {"id": "1", "nivel": {"id": "N1"}}, "serie": {"202610": "2.0"}}],
        }],
    }]
    payloads = iter((json.dumps(primary).encode(), json.dumps(candidate).encode()))
    monkeypatch.setattr(auditor, "_get", lambda *args, **kwargs: next(payloads))
    result = auditor._candidate_origin_audits(db, "2026-10-26T00:00:00+00:00")[0]
    assert result["status"] == "matched"
    assert result["independent_verification"] == "matched"


def test_bad_nested_shape_is_typed_candidate_parse_error():
    import pytest

    from source_audit.candidate_origins import CandidateOriginError, parse_sidra_v3

    with pytest.raises(CandidateOriginError, match="invalid_nested_shape"):
        parse_sidra_v3([{"id": "12607", "unidade": "Número-índice", "resultados": [None]}], required_identities=set(),
                       as_of=datetime.now(timezone.utc).date(), expected_unit="Número-índice",
                       expected_base_version="2022=100")
