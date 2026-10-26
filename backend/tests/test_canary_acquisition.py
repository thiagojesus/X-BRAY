import json
from datetime import date
from types import MappingProxyType

import pytest

from candidate_acquisition import CandidateAcquisitionService
from candidate_eligibility import (
    CandidateApproval,
    parser_semantic_sha256,
    profile_semantic_sha256,
)
from source_candidates import CandidateStatus, get_candidate_profile


def _service():
    from dataclasses import replace

    candidate_id = "bcb_sgs:csv:433"
    proof = "d" * 64
    profile = replace(get_candidate_profile(candidate_id), status=CandidateStatus.ELIGIBLE,
                      enabled_for_ingestion=True, code_reviewed=True,
                      approved_proof_sha256=proof, license_status="verified")
    approval = CandidateApproval(candidate_id, profile.source_contract_version,
        profile_semantic_sha256(profile), parser_semantic_sha256(candidate_id), proof,
        10, "2026-01-10", True, True, True)
    service = CandidateAcquisitionService(True, MappingProxyType({candidate_id: profile}),
                                          MappingProxyType({candidate_id: approval}))
    return profile, approval, service


def _sidra_service():
    from dataclasses import replace

    candidate_id = "ibge_sidra:v3:pim_industria"
    proof = "e" * 64
    profile = replace(get_candidate_profile(candidate_id), status=CandidateStatus.ELIGIBLE,
        enabled_for_ingestion=True, code_reviewed=True, approved_proof_sha256=proof,
        license_status="verified")
    approval = CandidateApproval(candidate_id, profile.source_contract_version,
        profile_semantic_sha256(profile), parser_semantic_sha256(candidate_id), proof,
        10, "2025-01-01", True, True, True)
    return profile, approval, CandidateAcquisitionService(True,
        MappingProxyType({candidate_id: profile}), MappingProxyType({candidate_id: approval}))


def _sidra_payloads():
    from datafetchers.ibge_sidra import SIDRA_DATASETS

    spec = SIDRA_DATASETS["pim_industria"]
    primary = [
        {"NC": "1", "NN": "Brasil", "MC": "Valor", "MN": "Variável",
         "V": "Valor", "D1C": "Brasil", "D1N": "Brasil", "D2C": "Variável",
         "D2N": "Variável", "D3C": "Mês", "D3N": "Mês", "D4C": "Categoria", "D4N": "Categoria"},
        {"NC": "1", "NN": "Brasil", "MC": "30", "MN": spec.unit_label,
         "V": "12,5", "D1C": "1", "D1N": "Brasil", "D2C": str(spec.variable_id),
         "D2N": spec.variable_label, "D3C": "202512", "D3N": "dezembro 2025",
         "D4C": str(spec.category_id), "D4N": spec.category_label},
    ]
    candidate = [{"id": str(spec.variable_id), "variavel": spec.variable_label,
        "unidade": spec.unit_label, "resultados": [{"classificacoes": [{
            "id": str(spec.classification_id), "categoria": {str(spec.category_id): spec.category_label}}],
            "series": [{"localidade": {"id": "1", "nome": "Brasil", "nivel": {"id": "N1", "nome": "Brasil"}},
                        "serie": {"202512": "12,5"}}]}]}]
    return primary, candidate


def test_sidra_paired_raw_acquisition_binds_candidate_to_primary_latest_period(monkeypatch):
    import canary_acquisition as canary
    from ingestion_policy import (
        RETRY_PROFILES,
        RetryBudget,
        install_retry_budget,
        reset_retry_budget,
    )

    profile, approval, service = _sidra_service()
    primary, candidate = _sidra_payloads()
    calls = []
    class Response:
        status_code = 200
        def __init__(self, body, url):
            self.content, self.url = body, url
            self.headers = {"content-type": "application/json"}
        def raise_for_status(self): pass
    budget = RetryBudget(RETRY_PROFILES["ibge_sidra"])
    def mocked(url, **kwargs):
        budget.reserve_request()
        calls.append((url, kwargs.get("params")))
        payload = primary if len(calls) == 1 else candidate
        return Response(json.dumps(payload).encode(), url)
    monkeypatch.setattr("provider_http.get_provider_response", mocked)
    token = install_retry_budget(budget)
    try:
        result = canary.acquire_sidra_canary(profile, approval, service, date(2025, 12, 31))
    finally:
        reset_retry_budget(token)
    assert len(calls) == budget.requests == 2
    assert "/p/last1" in calls[0][0]
    assert "/8888/periodos/202512/variaveis/12607" in calls[1][0]
    assert result.plan.reference_period == "202512"
    assert result.primary.observations[0]["value"] == result.candidate.observations[0]["value"] == 12.5


def test_sidra_unapproved_and_primary_failure_stop_before_candidate(monkeypatch):
    import canary_acquisition as canary
    from ingestion_policy import (
        RETRY_PROFILES,
        RetryBudget,
        install_retry_budget,
        reset_retry_budget,
    )

    profile, approval, service = _sidra_service()
    calls = []
    monkeypatch.setattr("provider_http.get_provider_response", lambda *a, **k: calls.append(1))
    with pytest.raises(canary.CanaryAcquisitionError):
        canary.acquire_sidra_canary(profile, approval, CandidateAcquisitionService(), date(2025, 12, 31))
    assert calls == []
    class Failed:
        status_code = 503
        content = b"{}"
        def __init__(self):
            self.headers = {}
        def raise_for_status(self):
            import httpx
            raise httpx.HTTPStatusError("failed", request=httpx.Request("GET", "https://sidra.ibge.gov.br"), response=httpx.Response(503))
    monkeypatch.setattr("provider_http.get_provider_response", lambda *a, **k: (calls.append(1), Failed())[1])
    budget = RetryBudget(RETRY_PROFILES["ibge_sidra"])
    token = install_retry_budget(budget)
    try:
        with pytest.raises(canary.CanaryAcquisitionError):
            canary.acquire_sidra_canary(profile, approval, service, date(2025, 12, 31))
    finally:
        reset_retry_budget(token)
    assert len(calls) == 1


def test_acquires_both_raw_responses_in_same_bounded_window(monkeypatch):
    import canary_acquisition as canary
    from ingestion_policy import (
        RETRY_PROFILES,
        RetryBudget,
        install_retry_budget,
        reset_retry_budget,
    )

    profile, approval, service = _service()
    rows = [{"data": f"{day:02d}/01/2026", "valor": str(day)} for day in range(1, 11)]
    csv = ("data;valor\n" + "\n".join(f"{d:02d}/01/2026;{d}" for d in range(1, 11))).encode()
    calls = []
    class Response:
        status_code = 200
        def __init__(self, body, url):
            self.content, self.url = body, url
            self.headers = {"content-type": "application/json"}
        def json(self): return json.loads(self.content)
        def raise_for_status(self): pass
    def response(url, **kwargs):
        budget.reserve_request()
        calls.append((url, kwargs["params"]))
        return Response(json.dumps(rows).encode() if len(calls) == 1 else csv, url)
    monkeypatch.setattr("provider_http.get_provider_response", response)
    budget = RetryBudget(RETRY_PROFILES["bcb_sgs"])
    token = install_retry_budget(budget)
    try:
        compared = canary.acquire_sgs_canary(profile, approval, service, date(2026, 1, 10))
    finally:
        reset_retry_budget(token)
    assert len(calls) == budget.requests == 2
    assert calls[0][1]["dataInicial"] == calls[1][1]["dataInicial"]
    assert calls[0][1]["dataFinal"] == calls[1][1]["dataFinal"]
    assert compared.plan.start == date(2025, 12, 11)
    assert compared.plan.end == date(2026, 1, 10)
    assert compared.captured_at.endswith("+00:00")


def test_unapproved_profile_fails_before_http(monkeypatch):
    import canary_acquisition as canary
    profile = get_candidate_profile("bcb_sgs:csv:433")
    called = []
    monkeypatch.setattr("provider_http.get_provider_response", lambda *a, **k: called.append(1))
    with pytest.raises(canary.CanaryAcquisitionError):
        canary.acquire_sgs_canary(profile, CandidateApproval("x", "", "", "", "", 0, "", False, False), CandidateAcquisitionService(), date(2026, 1, 10))
    assert called == []


def test_unsupported_tr226_rejected_before_http():
    from dataclasses import replace

    import canary_acquisition as canary
    profile = replace(get_candidate_profile("bcb_sgs:csv:226"))
    with pytest.raises(canary.CanaryAcquisitionError, match="dataFim"):
        canary.build_sgs_plan(profile, date(2026, 1, 10))
