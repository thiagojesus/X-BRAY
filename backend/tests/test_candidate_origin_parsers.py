import hashlib
from datetime import date

import pytest

from source_audit.candidate_origins import (
    CandidateOriginError,
    parse_sgs_csv,
    parse_sgs_json,
    parse_sidra_v3,
)


def test_official_sgs_csv_and_full_period_contract():
    identities = {("1", "2024-01-01")}
    rows = parse_sgs_csv(b"data;valor\n01/01/2024;2,0\n", contract_id="sgs:1", expected_unit="percentual", required_identities=identities, as_of=date(2024, 1, 1))
    assert rows[0]["value"] == "2"
    assert rows[0]["value_hash"] == hashlib.sha256(b"2.0").hexdigest()
    period = parse_sgs_csv(b"data;data fim;valor\n01/01/2024;01/02/2025;2\n", contract_id="sgs:226", expected_unit="pct_period", required_identities={("226", "2024-01-01", "2025-02-01")}, as_of=date(2024, 2, 1))
    assert period[0]["reference_end"] == "2025-02-01"


def test_csv_decodes_utf8_bom_and_normalizes_negative_zero():
    rows = parse_sgs_csv(
        b"\xef\xbb\xbfdata;valor\n01/01/2024;-0\n01/01/2024;-0\n",
        contract_id="sgs:1",
        expected_unit="percentual",
        required_identities={("1", "2024-01-01")},
        as_of=date(2024, 1, 1),
    )
    assert rows[0]["value"] == "0"


def test_csv_rejects_invalid_utf8_bytes():
    with pytest.raises(CandidateOriginError, match="invalid_csv"):
        parse_sgs_csv(
            b"data;valor\n\xff",
            contract_id="sgs:1",
            expected_unit="percentual",
            required_identities=set(),
            as_of=date(2024, 1, 1),
        )


@pytest.mark.parametrize("payload, contract, expected", [
    (b"data;valor\n1/1/2024;1\n", "sgs:1", "invalid_date"),
    (b"data;valor\n01/01/2024;1\n", "sgs:bogus", "unknown_contract"),
    (b"data;data fim;valor\n01/01/2024;;1\n", "sgs:226", "invalid_date"),
    (b"data;valor\n01/01/2024;1\n", "sgs:1", "incomplete_scope"),
])
def test_sgs_strict_scope_and_period(payload, contract, expected):
    req = {("226", "2024-01-01", "2024-01-01")} if contract == "sgs:226" else ({("1", "2024-01-01"), ("1", "2024-01-02")} if expected == "incomplete_scope" else {("1", "2024-01-01")})
    with pytest.raises(CandidateOriginError, match=expected):
        parse_sgs_csv(payload, contract_id=contract, expected_unit="u", required_identities=req, as_of=date(2024, 1, 2))


def sidra(value="-", *, variable="12607", unit="Número-índice", category="Indústria geral", locality="1", ref="202501"):
    return [{"id": variable, "variavel": "fixture", "unidade": unit, "resultados": [{
        "classificacoes": [{"id": "544", "categoria": {"129314": category}}],
        "series": [{"localidade": {"id": locality, "nivel": {"id": "N1"}}, "serie": {ref: value}}],
    }]}]


def sidra_id(ref="202501"):
    return ("12607", "8888", "N1", "1", "544", "129314", ref, "Número-índice", "2022=100")


def parse(data, ids=None):
    return parse_sidra_v3(data, required_identities={sidra_id()} if ids is None else ids, as_of=date(2025, 12, 1), expected_unit="Número-índice", expected_base_version="2022=100")


def test_real_v3_envelope_and_identity_table8888():
    parsed = parse(sidra("2"))
    assert parsed[0]["identity"] == sidra_id()
    assert parsed[0]["value_hash"] == hashlib.sha256(b"2.0").hexdigest()
    assert parse(sidra("2.0")) == parsed


@pytest.mark.parametrize("data, message", [
    ([{"D1C": "1", "D2C": "12607"}], "flattened_primary_not_v3"),
    (sidra(variable="8888"), "wrong_variable"),
    (sidra(unit="wrong"), "unit_or_base_mismatch"),
    (sidra(category="Other"), "wrong_territory_or_category"),
    (sidra(locality="2"), "wrong_territory_or_category"),
    (sidra(ref="202601"), "future_reference"),
    (sidra("X"), "unavailable_value"),
])
def test_v3_rejects_mismatches_and_unavailable(data, message):
    with pytest.raises(CandidateOriginError, match=message):
        parse(data)


def test_dash_is_absolute_zero_and_scope_is_complete():
    assert parse(sidra("-"))[0]["value"] == "0"
    with pytest.raises(CandidateOriginError, match="incomplete_scope"):
        parse(sidra("1"), ids={sidra_id(), sidra_id("202502")})


def test_parser_independent_of_producer(monkeypatch):
    import shadow_ingestion

    monkeypatch.setattr(shadow_ingestion, "parse_sidra_v3", lambda *args, **kwargs: (_ for _ in ()).throw(RuntimeError("wrong parser")), raising=False)
    assert parse(sidra("3"))[0]["value"] == "3"


def test_primary_sgs_json_representation_validates_exact_identity_and_value():
    required = {("1", "2024-01-01")}
    payload = b'[{"data":"01/01/2024","valor":"2,50"}]'
    records = parse_sgs_json(
        payload,
        contract_id="sgs:1",
        required_identities=required,
        as_of=date(2024, 1, 1),
        expected_unit="percentual",
    )
    assert records[0]["value"] == "2.5"
    assert records[0]["unit"] == "percentual"


@pytest.mark.parametrize(
    ("payload", "contract", "identities", "expected"),
    [
        (b"not-json", "sgs:1", {("1", "2024-01-01")}, "invalid_json"),
        (b"{}", "sgs:1", {("1", "2024-01-01")}, "invalid_shape"),
        (b"[null]", "sgs:1", {("1", "2024-01-01")}, "invalid_shape"),
        (b'[{"data":"01/01/2024","valor":"nope"}]', "sgs:1", {("1", "2024-01-01")}, "malformed_value"),
        (b'[{"data":"02/01/2024","valor":"1"}]', "sgs:1", {("1", "2024-01-01")}, "unexpected_identity"),
        (b'[{"data":"02/01/2024","dataFim":"01/01/2024","valor":"1"}]', "sgs:226", {("226", "2024-01-01", "2024-01-02")}, "invalid_period"),
    ],
)
def test_primary_sgs_json_rejects_malformed_and_out_of_scope_payloads(
    payload, contract, identities, expected
):
    with pytest.raises(CandidateOriginError, match=expected):
        parse_sgs_json(
            payload,
            contract_id=contract,
            required_identities=identities,
            as_of=date(2024, 1, 2),
            expected_unit="percentual",
        )


@pytest.mark.parametrize(
    ("payload", "contract", "expected"),
    [
        (b"data;valor;extra\n01/01/2024;1;x\n", "sgs:1", "unexpected_headers"),
        (b"data;valor\n01/01/2024;1\n", "sgs:226", "missing_period_end"),
        (b"data;data fim;valor\n01/01/2024;02/01/2024;1\n", "sgs:1", "unexpected_period_end"),
        (b"data;unidade;valor\n01/01/2024;wrong;1\n", "sgs:1", "unit_or_row_mismatch"),
        (b"data;valor\n01/01/2024;1\n01/01/2024;2\n", "sgs:1", "conflicting_duplicate"),
        (b"data;valor\n02/01/2024;1\n", "sgs:1", "future_reference"),
    ],
)
def test_candidate_csv_rejects_contract_boundary_violations(payload, contract, expected):
    identities = (
        {("226", "2024-01-01", "2024-01-02")}
        if contract == "sgs:226"
        else {("1", "2024-01-02")} if expected == "future_reference" else {("1", "2024-01-01")}
    )
    with pytest.raises(CandidateOriginError, match=expected):
        parse_sgs_csv(
            payload,
            contract_id=contract,
            expected_unit="expected",
            required_identities=identities,
            as_of=date(2024, 1, 1),
        )
