from __future__ import annotations

import json
from datetime import date, timedelta
from pathlib import Path
from unittest.mock import MagicMock

import httpx
import pytest

from db import store
from ingestion_policy import current_retry_budget
from shadow_ingestion import (
    CandidateBatch,
    CanonicalObservation,
    _batch_map,
    _month_range,
    _number,
    _sidra_v1_primary,
    compare_batches,
    parse_sgs_csv,
    parse_sidra_v3,
    run_shadow,
)
from source_candidates import get_candidate_profile
from source_contracts import CONTRACT_BY_ID

AS_OF = date(2026, 6, 30)


def _sgs_points(count: int = 12) -> list[dict[str, str]]:
    start = date(2025, 1, 1)
    return [
        {"data": (start + timedelta(days=index)).strftime("%d/%m/%Y"), "valor": f"{index + 0.5:.1f}"}
        for index in range(count)
    ]


def _profile_guard(monkeypatch: pytest.MonkeyPatch) -> None:
    def fail(*_args, **_kwargs):
        raise AssertionError("shadow mode must not use production database read/write functions")

    monkeypatch.setattr(store, "_execute", fail)
    monkeypatch.setattr(store, "execute_many", fail)
    monkeypatch.setattr(store, "_query", fail)


def test_sgs_public_shadow_runner_compares_real_http_boundary_and_writes_private_artifact(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    points = _sgs_points()
    csv_body = "data;valor\n" + "".join(f"{row['data']};{row['valor'].replace('.', ',')}\n" for row in points)
    requests: list[tuple[str, int, int, str | None, dict[str, object]]] = []

    def get(url: str, **kwargs) -> httpx.Response:
        budget = current_retry_budget()
        assert budget is not None
        requests.append((url, budget.requests, budget._request_limit,
                         budget._unit_context.get(), kwargs))
        payload = points if kwargs["params"].get("formato") == "json" else csv_body.encode()
        return httpx.Response(
            200,
            json=payload if isinstance(payload, list) else None,
            content=None if isinstance(payload, list) else payload,
            request=httpx.Request("GET", url),
        )

    _profile_guard(monkeypatch)
    monkeypatch.setattr(httpx, "get", get)
    artifact_path = tmp_path / "evidence.json"
    result = run_shadow("bcb_sgs:csv:433", AS_OF, artifact_path)

    assert result["status"] == "evidence_passed"
    assert result["production_eligible"] is False
    assert "license_evidence_unverified" in result["eligibility_blockers"]
    assert result["network"] == {"actual_outbound_requests": 2, "retry_requests": 0}
    assert result["source_table_writes"] == 0
    assert len(requests) == 2
    assert [call[1] for call in requests] == [1, 2]
    assert {call[2] for call in requests} == {3}
    assert {call[3] for call in requests} == {"shadow:primary", "shadow:candidate"}
    assert requests[0][4]["params"]["formato"] == "json"
    assert requests[1][4]["params"]["formato"] == "csv"
    assert artifact_path.stat().st_mode & 0o777 == 0o600
    saved = json.loads(artifact_path.read_text())
    assert saved["evidence"]["primary_distinct_references"] == 12
    assert saved["evidence"]["candidate_distinct_references"] == 12


def test_shadow_primary_rate_limit_never_dispatches_candidate_and_keeps_cooldown(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    requests: list[str] = []

    def get(url: str, **_kwargs) -> httpx.Response:
        requests.append(url)
        return httpx.Response(
            429,
            headers={"Retry-After": "3600"},
            request=httpx.Request("GET", url),
        )

    _profile_guard(monkeypatch)
    monkeypatch.setattr(httpx, "get", get)
    result = run_shadow("bcb_sgs:csv:433", AS_OF, tmp_path / "limited.json")
    assert result["status"] == "acquisition_failed"
    assert result["error"]["category"] == "rate_limited"
    assert result["error"]["retry_not_before"]
    assert result["network"]["actual_outbound_requests"] == 1
    assert len(requests) == 1


def test_disabled_source_descriptor_has_no_factory_and_never_dispatches_network(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    network = MagicMock(side_effect=AssertionError("disabled descriptor must not dispatch"))
    monkeypatch.setattr(httpx, "get", network)
    result = run_shadow("anbima:txt:unverified", AS_OF, tmp_path / "unsupported.json")
    assert result["status"] == "unsupported"
    assert result["network"]["actual_outbound_requests"] == 0
    assert result["production_eligible"] is False
    network.assert_not_called()


def test_sgs_csv_parser_requires_full_tr_period_identity_and_keeps_future_end() -> None:
    profile = get_candidate_profile("bcb_sgs:csv:226")
    rows = []
    start = date(2025, 1, 1)
    for index in range(10):
        period_start = start + timedelta(days=index * 30)
        rows.append(
            f"{period_start:%d/%m/%Y};{(period_start + timedelta(days=45)):%d/%m/%Y};0,00"
        )
    body = ("data;dataFim;valor\n" + "\n".join(rows)).encode()
    observations = parse_sgs_csv(profile, body, as_of=AS_OF)
    assert len(observations) == 10
    assert all(dict(item.identity)["period_end"] for item in observations)
    assert dict(observations[0].values)["value"] == 0.0

    with pytest.raises(ValueError, match="header"):
        parse_sgs_csv(
            profile,
            b"data;valor\n01/01/2025;0,1\n",
            as_of=AS_OF,
        )


def test_sgs_csv_parser_rejects_future_start_dates_and_invalid_numeric_values() -> None:
    profile = get_candidate_profile("bcb_sgs:csv:433")
    with pytest.raises(ValueError, match="after the as-of cutoff"):
        parse_sgs_csv(profile, b"data;valor\n01/07/2026;0,1\n", as_of=AS_OF)
    with pytest.raises(ValueError):
        parse_sgs_csv(profile, b"data;valor\n01/01/2025;NaN\n", as_of=AS_OF)


def test_sidra_v3_parser_checks_full_profile_and_does_not_turn_missing_values_into_zero() -> None:
    profile = get_candidate_profile("ibge_sidra:v3:pim_industria")
    from datafetchers.ibge_sidra import SIDRA_DATASETS

    spec = SIDRA_DATASETS["pim_industria"]
    periods = {
        "202501": "-", "202502": "0", "202503": "0,00",
        "202504": "-0.12", "202505": "-0,12", "202506": "...",
        "202507": "X", "202508": "8", "202509": "9", "202510": "10",
        "202511": "11", "202512": "12",
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
                "localidade": {"id": "1", "nome": "Brasil", "nivel": {"id": "N1", "nome": "Brasil"}},
                "serie": periods,
            }],
        }],
    }]
    observations = parse_sidra_v3(profile, spec, json.dumps(payload).encode(), as_of=AS_OF)
    assert len(observations) == 10
    values_by_date = {item.reference_date: dict(item.values)["value"] for item in observations}
    assert values_by_date["2025-01-01"] == 0.0
    assert values_by_date["2025-02-01"] == 0.0
    assert values_by_date["2025-03-01"] == 0.0
    assert values_by_date["2025-04-01"] == -0.12
    assert values_by_date["2025-05-01"] == -0.12
    assert "2025-06-01" not in values_by_date
    assert "2025-07-01" not in values_by_date
    with pytest.raises(ValueError, match="unit"):
        bad = [{**payload[0], "unidade": "unknown"}]
        parse_sidra_v3(profile, spec, json.dumps(bad).encode(), as_of=AS_OF)
    with pytest.raises(ValueError, match="variable id"):
        bad = [{**payload[0], "id": str(spec.table_id)}]
        parse_sidra_v3(profile, spec, json.dumps(bad).encode(), as_of=AS_OF)
    bad_classification = json.loads(json.dumps(payload))
    bad_classification[0]["resultados"][0]["classificacoes"][0]["categoria"] = {
        "999999": spec.category_label
    }
    with pytest.raises(ValueError, match="category"):
        parse_sidra_v3(profile, spec, json.dumps(bad_classification).encode(), as_of=AS_OF)
    bad_classification[0]["resultados"][0]["classificacoes"][0]["id"] = "999999"
    with pytest.raises(ValueError, match="category"):
        parse_sidra_v3(profile, spec, json.dumps(bad_classification).encode(), as_of=AS_OF)
    cosmetic_category = json.loads(json.dumps(payload))
    cosmetic_category[0]["resultados"][0]["classificacoes"][0]["categoria"] = {
        str(spec.category_id): " 1 INDU\u0301STRIA   GERAL "
    }
    assert parse_sidra_v3(
        profile, spec, json.dumps(cosmetic_category).encode(), as_of=AS_OF
    ) == observations
    for invalid_category in ("Total...", None, "", 129314):
        invalid_label = json.loads(json.dumps(payload))
        invalid_label[0]["resultados"][0]["classificacoes"][0]["categoria"] = {
            str(spec.category_id): invalid_category
        }
        with pytest.raises(ValueError, match="category"):
            parse_sidra_v3(profile, spec, json.dumps(invalid_label).encode(), as_of=AS_OF)
    bad_geography = json.loads(json.dumps(payload))
    bad_geography[0]["resultados"][0]["series"][0]["localidade"]["id"] = "2"
    with pytest.raises(ValueError, match="geography"):
        parse_sidra_v3(profile, spec, json.dumps(bad_geography).encode(), as_of=AS_OF)


def test_sidra_v1_shadow_primary_preserves_dash_as_zero_without_imputing_sentinels() -> None:
    from datafetchers.ibge_sidra import SIDRA_DATASETS

    spec = SIDRA_DATASETS["pim_industria"]
    profile = get_candidate_profile("ibge_sidra:v3:pim_industria")
    header = {
        key: label for key, label in {
            "NC": "Nível Territorial", "NN": "Nível Territorial (Nome)",
            "MC": "Unidade de Medida", "MN": "Unidade de Medida (Nome)",
            "V": "Valor", "D1C": "Brasil", "D1N": "Brasil",
            "D2C": "Variável", "D2N": "Variável (Nome)",
            "D3C": "Mês", "D3N": "Mês (Código)",
            "D4C": "Atividade", "D4N": "Atividade (Código)",
        }.items()
    }
    row = {
        "NC": "1", "NN": "Brasil", "MC": spec.unit_code,
        "MN": spec.unit_label, "V": "-", "D1C": "1", "D1N": "Brasil",
        "D2C": str(spec.variable_id), "D2N": spec.variable_label,
        "D3C": "202501", "D3N": "01/2025",
        "D4C": str(spec.category_id), "D4N": spec.category_label,
    }
    result = _sidra_v1_primary(
        profile, spec, json.dumps([header, row]).encode(), as_of=AS_OF
    )
    assert len(result) == 1
    assert dict(result[0].values)["value"] == 0.0

    missing_row = {**row, "V": "..."}
    with pytest.raises(ValueError, match="no usable observations"):
        _sidra_v1_primary(
            profile, spec, json.dumps([header, missing_row]).encode(), as_of=AS_OF
        )


def _observation(contract_id: str, ref: str, value: float, *, end: str | None = None) -> CanonicalObservation:
    contract = CONTRACT_BY_ID[contract_id]
    if contract_id == "sgs:226":
        identity = (("series_code", "226"), ("period_start", ref), ("period_end", end or "2025-01-31"))
    else:
        identity = (("series_code", contract_id.removeprefix("sgs:")), ("reference_date", ref))
    return CanonicalObservation(
        contract_id, ref, identity, ((contract.value_fields[0], value),), contract.unit, contract.frequency
    )


def test_equivalence_evidence_requires_distinct_references_and_full_tr_identity() -> None:
    profile = get_candidate_profile("bcb_sgs:csv:226")
    references = [date(2025, 1, 1) + timedelta(days=30 * i) for i in range(10)]
    primary_rows = tuple(_observation("sgs:226", item.isoformat(), float(i), end="2025-02-01")
                         for i, item in enumerate(references))
    candidate_rows = tuple(_observation("sgs:226", item.isoformat(), float(i), end="2025-02-02")
                           for i, item in enumerate(references))
    primary = CandidateBatch(profile.candidate_id, ("sgs:226",), primary_rows, "a" * 64, "b" * 64, "api.bcb.gov.br")
    candidate = CandidateBatch(profile.candidate_id, ("sgs:226",), candidate_rows, "c" * 64, "d" * 64, "api.bcb.gov.br")
    evidence = compare_batches(profile, primary, candidate, as_of=AS_OF)
    assert evidence.evidence_status == "evidence_failed"
    assert "full_identity_cohort_mismatch" in evidence.mismatches
    assert not evidence.production_eligible

    too_few = CandidateBatch(profile.candidate_id, ("sgs:226",), primary_rows[:9], "a" * 64, "b" * 64, "api.bcb.gov.br")
    too_few_evidence = compare_batches(profile, too_few, too_few, as_of=AS_OF)
    assert "primary_has_fewer_than_10_distinct_reference_dates" in too_few_evidence.mismatches


def test_candidate_batch_cannot_forge_publication_time_or_leave_contract_fields_out() -> None:
    profile = get_candidate_profile("bcb_sgs:csv:433")
    row = _observation("sgs:433", "2025-01-01", 0.0)
    forged = CanonicalObservation(
        row.contract_id, row.reference_date, row.identity, row.values, row.unit, row.frequency,
        source_published_at="2025-01-01T00:00:00Z",
    )
    one = CandidateBatch(profile.candidate_id, ("sgs:433",), (forged,), "a" * 64, "b" * 64, "api.bcb.gov.br")
    with pytest.raises(ValueError, match="publication time"):
        compare_batches(profile, one, one, as_of=AS_OF)

    missing = CanonicalObservation(
        "sgs:433", "2025-01-01", (), (("value", 0.0),), "pct", "daily"
    )
    malformed = CandidateBatch(profile.candidate_id, ("sgs:433",), (missing,), "a" * 64, "b" * 64, "api.bcb.gov.br")
    with pytest.raises(ValueError, match="dimensions"):
        compare_batches(profile, malformed, malformed, as_of=AS_OF)


def test_candidate_profile_id_and_host_must_match_evidence_batches() -> None:
    profile = get_candidate_profile("bcb_sgs:csv:433")
    row = _observation("sgs:433", "2025-01-01", 1.0)
    batch = CandidateBatch("wrong", ("sgs:433",), (row,), "a" * 64, "b" * 64, "attacker.invalid")
    evidence = compare_batches(profile, batch, batch, as_of=AS_OF)
    assert "candidate_profile_id_mismatch" in evidence.mismatches
    assert "candidate_endpoint_host_mismatch" in evidence.mismatches
    assert not evidence.production_eligible


def test_artifact_writer_refuses_overwrite(tmp_path: Path) -> None:
    from shadow_ingestion import _write_private_artifact

    path = tmp_path / "existing.json"
    path.write_text("leave me")
    with pytest.raises(FileExistsError):
        _write_private_artifact(path, {"status": "test"})
    assert path.read_text() == "leave me"


def test_sidra_primary_and_v3_shadow_runner_uses_bounded_two_request_readonly_flow(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from datafetchers.ibge_sidra import SIDRA_DATASETS

    profile = get_candidate_profile("ibge_sidra:v3:pim_industria")
    spec = SIDRA_DATASETS["pim_industria"]
    header = {
        "NC": "Nível Territorial", "NN": "Nível Territorial (Nome)",
        "MC": "Unidade de Medida", "MN": "Unidade de Medida (Nome)",
        "V": "Valor", "D1C": "Brasil", "D1N": "Brasil",
        "D2C": "Variável", "D2N": "Variável (Nome)",
        "D3C": "Mês", "D3N": "Mês (Código)",
        "D4C": "Atividade", "D4N": "Atividade (Código)",
    }
    primary_payload: list[dict[str, str]] = [header]
    v3_periods: dict[str, str] = {}
    begin = date(2025, 1, 1)
    for index in range(12):
        current = date(begin.year + (begin.month + index - 1) // 12,
                       (begin.month + index - 1) % 12 + 1, 1)
        period = current.strftime("%Y%m")
        value = str(index + 1)
        primary_payload.append({
            "NC": "1", "NN": "Brasil", "MC": spec.unit_code,
            "MN": spec.unit_label, "V": "Valor", "D1C": "1",
            "D1N": "Brasil", "D2C": str(spec.variable_id),
            "D2N": spec.variable_label, "D3C": period,
            "D3N": f"{current:%m/%Y}", "D4C": str(spec.category_id),
            "D4N": spec.category_label,
        } | {"V": value})
        v3_periods[period] = value
    v3_payload = [{
        "id": str(spec.variable_id), "variavel": spec.variable_label,
        "unidade": spec.unit_label,
        "resultados": [{
            "classificacoes": [{"id": str(spec.classification_id),
                                "categoria": {str(spec.category_id): spec.category_label}}],
            "series": [{"localidade": {
                "id": "1", "nome": "Brasil", "nivel": {"id": "N1", "nome": "Brasil"}},
                "serie": v3_periods}],
        }],
    }]
    outbound: list[tuple[str, dict[str, str]]] = []

    def get_response(url: str, **kwargs) -> httpx.Response:
        outbound.append((url, kwargs["params"]))
        payload = primary_payload if "apisidra" in url else v3_payload
        return httpx.Response(200, content=json.dumps(payload).encode(),
                              request=httpx.Request("GET", url))

    _profile_guard(monkeypatch)
    monkeypatch.setattr(httpx, "get", get_response)
    result = run_shadow(profile.candidate_id, AS_OF, tmp_path / "sidra.json")
    assert result["status"] == "evidence_passed"
    assert result["network"]["actual_outbound_requests"] == 2
    assert result["source_table_writes"] == 0
    assert len(outbound) == 2
    assert outbound[0][0].startswith("https://apisidra.ibge.gov.br")
    assert outbound[1][0].startswith("https://servicodados.ibge.gov.br")
    assert outbound[1][1]["localidades"] == "N1[1]"


def test_sidra_candidate_schema_error_reports_parse_stage_without_raw_payload(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from datafetchers.ibge_sidra import SIDRA_DATASETS

    profile = get_candidate_profile("ibge_sidra:v3:pim_industria")
    spec = SIDRA_DATASETS["pim_industria"]
    primary_payload = [{
        "NC": "Nível Territorial", "NN": "Nível Territorial (Nome)",
        "MC": "Unidade de Medida", "MN": "Unidade de Medida (Nome)",
        "V": "Valor", "D1C": "Brasil", "D1N": "Brasil",
        "D2C": "Variável", "D2N": "Variável (Nome)",
        "D3C": "Mês", "D3N": "Mês (Código)",
        "D4C": "Atividade", "D4N": "Atividade (Código)",
    }]
    for month in range(1, 13):
        period = date(2025, month, 1)
        primary_payload.append({
            "NC": "1", "NN": "Brasil", "MC": spec.unit_code,
            "MN": spec.unit_label, "V": "1", "D1C": "1",
            "D1N": "Brasil", "D2C": str(spec.variable_id),
            "D2N": spec.variable_label, "D3C": period.strftime("%Y%m"),
            "D3N": period.strftime("%m/%Y"), "D4C": str(spec.category_id),
            "D4N": spec.category_label,
        })
    bad_payload = [{
        "id": str(spec.variable_id),
        "variavel": spec.variable_label,
        "unidade": spec.unit_label,
        "resultados": [{
            "classificacoes": [{
                "id": str(spec.classification_id),
                "categoria": {str(spec.category_id): "Total da indústria"},
            }],
            "series": [{
                "localidade": {
                    "id": "1", "nome": "Brasil",
                    "nivel": {"id": "N1", "nome": "Brasil"},
                },
                "serie": {date(2025, month, 1).strftime("%Y%m"): "1"
                          for month in range(1, 13)},
            }],
        }],
    }]

    def get_response(url: str, **kwargs) -> httpx.Response:
        if "apisidra" in url:
            payload = primary_payload
        else:
            payload = bad_payload
        return httpx.Response(200, content=json.dumps(payload).encode(),
                              request=httpx.Request("GET", url))

    _profile_guard(monkeypatch)
    monkeypatch.setattr(httpx, "get", get_response)
    artifact = run_shadow(profile.candidate_id, AS_OF, tmp_path / "invalid-sidra.json")

    assert artifact["status"] == "acquisition_failed"
    assert artifact["error"] == {
        "type": "ShadowRunError",
        "category": "invalid_schema",
        "code": "sidra_candidate_payload_invalid",
        "stage": "candidate_parse",
    }
    assert artifact["network"]["actual_outbound_requests"] == 2
    assert artifact["source_table_writes"] == 0
    assert "resultados" not in json.dumps(artifact)


def test_sidra_primary_schema_error_reports_stage_and_stops_before_candidate_request(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    profile = get_candidate_profile("ibge_sidra:v3:pim_industria")
    requests: list[str] = []

    def get_response(url: str, **kwargs) -> httpx.Response:
        requests.append(url)
        return httpx.Response(
            200,
            content=b"[]",
            request=httpx.Request("GET", url),
        )

    _profile_guard(monkeypatch)
    monkeypatch.setattr(httpx, "get", get_response)
    artifact = run_shadow(profile.candidate_id, AS_OF, tmp_path / "invalid-primary.json")

    assert artifact["error"]["category"] == "invalid_schema"
    assert artifact["error"]["code"] == "sidra_primary_payload_invalid"
    assert artifact["error"]["stage"] == "primary_parse"
    assert artifact["network"]["actual_outbound_requests"] == 1
    assert len(requests) == 1
    assert artifact["source_table_writes"] == 0


def test_unexpected_shadow_value_error_remains_internal_error(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import shadow_ingestion

    profile = get_candidate_profile("ibge_sidra:v3:pim_industria")

    def unexpected_error(*_args):
        raise ValueError("unexpected implementation defect")

    monkeypatch.setattr(shadow_ingestion, "_collect_sidra", unexpected_error)
    artifact = run_shadow(profile.candidate_id, AS_OF, tmp_path / "internal-error.json")

    assert artifact["error"]["category"] == "internal_error"
    assert artifact["error"]["type"] == "ValueError"
    assert "stage" not in artifact["error"]


def test_shadow_cli_reports_success_failure_and_sanitized_local_errors(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    import shadow_cli

    args = ["shadow", "--candidate", "bcb_sgs:csv:433", "--as-of", "2026-06-30",
            "--artifact", str(tmp_path / "cli.json")]
    monkeypatch.setattr(shadow_cli, "run_shadow", lambda *_: {"status": "evidence_passed"})
    assert shadow_cli.main(args) == 0
    success = json.loads(capsys.readouterr().out)
    assert success["status"] == "evidence_passed"
    assert success["production_eligible"] is False

    monkeypatch.setattr(shadow_cli, "run_shadow", lambda *_: {"status": "unsupported"})
    assert shadow_cli.main(args) == 2
    assert json.loads(capsys.readouterr().out)["status"] == "unsupported"

    def local_error(*_args):
        raise OSError("local filesystem diagnostic must not be echoed")

    monkeypatch.setattr(shadow_cli, "run_shadow", local_error)
    assert shadow_cli.main(args) == 2
    error = capsys.readouterr().err
    assert "OSError" in error
    assert "local filesystem diagnostic" not in error

    with pytest.raises(SystemExit):
        shadow_cli.main(["shadow", "--candidate", "https://attacker.invalid", "--as-of",
                         "2026-06-30", "--artifact", str(tmp_path / "bad.json")])
    runner = MagicMock(return_value={"status": "evidence_passed"})
    monkeypatch.setattr(shadow_cli, "run_shadow", runner)
    for invalid_date in (
        "20260630", "2026-W27-2", "2026-06-30T00:00:00", "2026-6-30",
        " 2026-06-30", "2026-06-30 ", "２０２６-０６-３０", "2026-02-30", "2026",
    ):
        with pytest.raises(SystemExit):
            shadow_cli.main(["shadow", "--candidate", "bcb_sgs:csv:433", "--as-of",
                             invalid_date, "--artifact", str(tmp_path / "bad-date.json")])
    runner.assert_not_called()


def test_shadow_batch_validation_rejects_each_malformed_dimension() -> None:
    from dataclasses import replace

    profile = get_candidate_profile("bcb_sgs:csv:433")
    valid = _observation("sgs:433", "2025-01-01", 1.0)

    def check(observation: CanonicalObservation, contract_ids: tuple[str, ...] = ("sgs:433",)) -> None:
        batch = CandidateBatch(profile.candidate_id, contract_ids, (observation,),
                               "a" * 64, "b" * 64, "api.bcb.gov.br")
        _batch_map(batch, AS_OF)

    with pytest.raises(ValueError, match="no source contract"):
        check(valid, ())
    with pytest.raises(ValueError, match="unplanned contract"):
        check(valid, ("sgs:226",))
    with pytest.raises(ValueError, match="canonical ISO"):
        check(replace(valid, reference_date="20250101"))
    with pytest.raises(ValueError, match="post-cutoff"):
        check(replace(valid, reference_date="2026-07-01", identity=(
            ("series_code", "433"), ("reference_date", "2026-07-01")
        )))
    with pytest.raises(ValueError, match="value fields"):
        check(replace(valid, values=(("unexpected", 1.0),)))
    with pytest.raises(ValueError, match="identity"):
        check(replace(valid, identity=(("series_code", "433"),)))
    with pytest.raises(ValueError, match="unit or frequency"):
        check(replace(valid, unit="wrong"))
    with pytest.raises(ValueError, match="non-finite"):
        check(replace(valid, values=(("value", float("inf")),)))
    with pytest.raises(ValueError, match="duplicate"):
        _batch_map(CandidateBatch(profile.candidate_id, ("sgs:433",),
                                  (valid, valid), "a" * 64, "b" * 64, "api.bcb.gov.br"), AS_OF)


def test_shadow_calendar_and_parsers_reject_invalid_dates_values_and_conflicts() -> None:
    profile = get_candidate_profile("bcb_sgs:csv:433")
    assert _month_range(date(2026, 1, 15), 3) == ("202511", "202601")
    assert parse_sgs_csv(profile, b"data;valor\n01/01/2025;1.25\n", as_of=AS_OF)[0].values == (("value", 1.25),)
    invalid_csv_samples = (
        b"data;valor\n31/02/2025;1\n",
        b"data;valor\n01/01/2025;bad\n",
        b"data;valor\n01/01/2025;1\n01/01/2025;2\n",
        b"data;valor\n",
        b"data;valor\n01/01/2025;\n",
    )
    for sample in invalid_csv_samples:
        with pytest.raises(ValueError):
            parse_sgs_csv(profile, sample, as_of=AS_OF)
    with pytest.raises(ValueError, match="canonical dd/mm/YYYY"):
        parse_sgs_csv(profile, b"data;valor\n01/1/2025;1\n", as_of=AS_OF)
    with pytest.raises(ValueError, match="utf-8"):
        parse_sgs_csv(profile, b"\xff", as_of=AS_OF)
    for invalid in (True, object(), float("nan"), float("inf")):
        with pytest.raises(ValueError):
            _number(invalid)


def test_sidra_v3_decoder_rejects_invalid_json_envelope_and_dataset_dimensions() -> None:
    from datafetchers.ibge_sidra import SIDRA_DATASETS

    profile = get_candidate_profile("ibge_sidra:v3:pim_industria")
    spec = SIDRA_DATASETS["pim_industria"]
    bad_payloads = (
        b"not-json",
        b"{}",
        json.dumps([{"id": "wrong"}]).encode(),
        json.dumps([{"id": str(spec.variable_id), "unidade": "wrong",
                     "variavel": spec.variable_label}]).encode(),
        json.dumps([{"id": str(spec.variable_id), "unidade": spec.unit_label,
                     "variavel": spec.variable_label, "resultados": []}]).encode(),
        json.dumps([{"id": str(spec.variable_id), "unidade": spec.unit_label,
                     "variavel": spec.variable_label,
                      "resultados": [{"classificacoes": [], "series": []}]}]).encode(),
    )
    for body in bad_payloads:
        with pytest.raises((ValueError, json.JSONDecodeError)):
            parse_sidra_v3(profile, spec, body, as_of=AS_OF)


def test_equivalence_marks_stale_versions_latest_mismatch_and_value_drift() -> None:
    from dataclasses import replace

    profile = get_candidate_profile("bcb_sgs:csv:433")
    rows = tuple(_observation("sgs:433", (date(2025, 1, 1) + timedelta(days=index)).isoformat(),
                              float(index)) for index in range(12))
    primary = CandidateBatch(profile.candidate_id, ("sgs:433",), rows,
                             "a" * 64, "b" * 64, "api.bcb.gov.br")
    drift_rows = (*rows[:-1], replace(rows[-1], values=(("value", 999.0),)))
    candidate = CandidateBatch(profile.candidate_id, ("sgs:433",), drift_rows,
                               "c" * 64, "d" * 64, "api.bcb.gov.br")
    stale_profile = replace(profile, source_contract_version="old-contract", code_version="old-code")
    evidence = compare_batches(stale_profile, primary, candidate, as_of=AS_OF)
    assert "stale_source_contract_version" in evidence.mismatches
    assert "stale_candidate_code_version" in evidence.mismatches
    assert "2025-01-12:value_mismatch_value" in evidence.mismatches
    assert not evidence.production_eligible

    old_candidate = CandidateBatch(profile.candidate_id, ("sgs:433",), rows[:-1],
                                   "e" * 64, "f" * 64, "api.bcb.gov.br")
    latest = compare_batches(profile, primary, old_candidate, as_of=AS_OF)
    assert "latest_reference_date_mismatch" in latest.mismatches


def test_equivalence_keeps_official_sidra_zero_points_in_ten_date_cohort() -> None:
    profile = get_candidate_profile("ibge_sidra:v3:pim_industria")
    from datafetchers.ibge_sidra import SIDRA_DATASETS

    spec = SIDRA_DATASETS["pim_industria"]
    reference_dates = tuple(date(2025 + index // 12, index % 12 + 1, 1) for index in range(10))
    observations = tuple(
        CanonicalObservation(
            contract_id=profile.source_contract_ids[0],
            reference_date=reference_date.isoformat(),
            identity=(
                ("table_id", str(spec.table_id)),
                ("variable_id", str(spec.variable_id)),
                ("territory_level", spec.territory_level),
                ("territory_code", spec.territory_code),
                ("classification_id", str(spec.classification_id)),
                ("category_id", str(spec.category_id)),
                ("reference_period", reference_date.strftime("%Y%m")),
                ("unit", spec.unit),
                ("base_version", str(spec.base_version)),
            ),
            values=(("value", 0.0),),
            unit=spec.unit,
            frequency=spec.frequency,
        )
        for reference_date in reference_dates
    )
    primary = CandidateBatch(profile.candidate_id, profile.source_contract_ids,
                             observations, "a" * 64, "b" * 64, "apisidra.ibge.gov.br")
    candidate = CandidateBatch(profile.candidate_id, profile.source_contract_ids,
                               observations, "c" * 64, "d" * 64,
                               "servicodados.ibge.gov.br")
    evidence = compare_batches(profile, primary, candidate, as_of=AS_OF)
    assert evidence.evidence_status == "evidence_passed"
    assert evidence.primary_distinct_references == 10
    assert evidence.candidate_distinct_references == 10
    assert evidence.matched_observation_count == 10
    assert all(dict(row.values)["value"] == 0.0 for row in evidence.candidate_observations)
