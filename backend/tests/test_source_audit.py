from __future__ import annotations

import io
import types
import zipfile
from datetime import datetime, timedelta, timezone
from unittest.mock import patch

import pytest

from source_audit import auditor
from source_audit.catalog import SGS_CODES, SIDRA_DATASETS, inventory


def test_catalog_covers_every_current_source_family_and_sgs_series() -> None:
    catalog = inventory()
    assert len(catalog["sgs"]) == 64
    assert len({item["provider_code"] for item in catalog["sgs"]}) == 64
    assert len(catalog["focus_annual"]) == 5
    assert len(catalog["sidra"]) == 5
    assert {"anbima", "treasury", "b3_di"} <= catalog.keys()
    assert "Polymarket" in catalog["excluded"][0]
    assert {group for item in catalog["sgs"] for group in item["refresh_groups"]} == {
        "juros", "inflacao", "atividade", "fiscal", "credito", "cambio",
        "setor_externo", "complementares", "ipca_grupos", "ipca_naturezas",
        "ipca_nucleos", "ipca_precos",
    }


def test_catalog_matches_live_refresh_and_frontend_retirement_surface() -> None:
    from config import (
        ACTIVITY_REFRESH,
        COMPLEMENTARY_REFRESH,
        CREDIT,
        EXCHANGE,
        FISCAL,
        INFLATION,
        INTEREST_RATES,
        IPCA_CORE,
        IPCA_GROUPS,
        IPCA_NATURE,
        IPCA_PRICES,
        SETOR_EXTERNO,
    )
    from refresh import RefreshDependencies, _tasks

    active = {int(code) for mapping in (
        ACTIVITY_REFRESH, COMPLEMENTARY_REFRESH, CREDIT, EXCHANGE, FISCAL,
        INFLATION, INTEREST_RATES, IPCA_CORE, IPCA_GROUPS, IPCA_NATURE,
        IPCA_PRICES, SETOR_EXTERNO,
    ) for code in mapping.values()}
    assert active == set(SGS_CODES)
    assert len(SIDRA_DATASETS) == 5
    assert all(task.name != "polymarket" for task in _tasks(True, RefreshDependencies()))


@pytest.mark.parametrize(
    ("source_ref", "db_ref", "source_value", "db_value", "expected"),
    [
        ("2026-10-02", "2026-10-01", "1", 1.0, "source_newer"),
        ("2026-10-01", "2026-10-02", "1", 1.0, "database_ahead"),
        ("2026-10-02", "2026-10-02", "1.0000000000000000", 1.0, "matched"),
        ("2026-10-02", "2026-10-02", "1.1", 1.0, "same_reference_value_mismatch"),
        ("2026-10-02", None, "1", None, "database_missing"),
        (None, "2026-10-02", None, 1.0, "source_unavailable"),
    ],
)
def test_compare_observation_statuses(source_ref, db_ref, source_value, db_value, expected) -> None:
    result = auditor.compare_observation(
        source_ref, source_value, db_ref, db_value, source_checked_at="2026-10-24T12:00:00Z", identity="test"
    )
    assert result["status"] == expected


def test_latest_valid_sgs_skips_invalid_trailing_record() -> None:
    latest = auditor._valid_latest(
        [{"data": "01/10/2026", "valor": "2.5"}, {"data": "bad", "valor": "9"}],
        "data", "valor",
    )
    assert latest == {"data": "01/10/2026", "valor": "2.5"}


@pytest.mark.parametrize(("value", "expected"), [
    (None, None), (True, None), ("n/a", None), ("NaN", None), ("1,25", "1.25"),
])
def test_number_parser_rejects_unusable_values_and_normalizes_decimal_comma(value, expected) -> None:
    result = auditor._number(value)
    assert (str(result) if result is not None else None) == expected


@pytest.mark.parametrize(("value", "expected"), [
    ("202610", "2026-10-01"), ("2026", "2026-01-01"),
    ("24/10/2026", "2026-10-24"), ("20261024", "2026-10-24"),
    ("not-a-date", None), (None, None),
])
def test_date_parser_handles_provider_period_formats(value, expected) -> None:
    result = auditor._date(value)
    assert (result.isoformat() if result else None) == expected


def test_json_adapter_bounds_payload_and_rejects_malformed_json(monkeypatch) -> None:
    monkeypatch.setattr(auditor, "_get", lambda *_args, **_kwargs: b"{broken")
    with pytest.raises(ValueError, match="invalid_source_json"):
        auditor._json("https://example.test")


def test_focus_keeps_distinct_annual_horizons_and_exact_latest_vintages() -> None:
    def response(url, params):
        indicator = params["$filter"].split("'")[1]
        return {"value": [
            {"Indicador": indicator, "DataReferencia": "2026", "baseCalculo": 0, "Suavizada": "N", "Data": "2026-10-20", "Mediana": 4.1},
            {"Indicador": indicator, "DataReferencia": "2027", "baseCalculo": 0, "Suavizada": "N", "Data": "2026-10-21", "Mediana": 3.9},
            {"Indicador": indicator, "DataReferencia": "2027", "baseCalculo": 1, "Suavizada": "N", "Data": "2026-10-22", "Mediana": 8.8},
        ]}
    with patch.object(auditor, "_json", side_effect=response):
        results = auditor._probe_focus({"focus_vintages": []}, "checked")
    annual_ipca = [row for row in results if row["identity"].startswith("focus:IPCA:")]
    assert len(annual_ipca) == 3
    assert {row["source_target"] for row in annual_ipca} == {"2026", "2027"}
    assert any("|1|" in row["identity"] for row in annual_ipca)


def test_b3_parser_rejects_corrupt_zip_and_archive_limits() -> None:
    with pytest.raises(zipfile.BadZipFile):
        auditor._parse_b3(b"not-a-zip", {})
    archive = io.BytesIO()
    with zipfile.ZipFile(archive, "w") as zipped:
        for index in range(101):
            zipped.writestr(f"{index}.txt", b"x")
    with pytest.raises(ValueError, match="archive_limits"):
        auditor._parse_b3(archive.getvalue(), {})


def test_sidra_zero_dash_is_valid_and_placeholders_are_skipped() -> None:
    rows = auditor._sidra_rows([
        {"D1C": "1", "D3C": "202608", "V": "...", "MC": "30"},
        {"D1C": "1", "D3C": "202607", "V": "-", "MC": "30"},
        {"D1C": "1", "D3C": "202609", "V": "8", "MC": "30"},
    ], {"territory_code": "1"}).observations
    assert rows == [{
        "reference_period": "202609", "value": "8", "unit_code": "30",
        "row": {"D1C": "1", "D3C": "202609", "V": "8", "MC": "30"},
        "latest_source_period": "202609", "unavailable_newer_rows": 0,
    }]
    assert any(item["value"] == "0" for item in rows) is False
    zero = auditor._sidra_rows([{"header": "value"}, {"D1C": "1", "D3C": "202607", "V": "-", "MC": "30"}], {"territory_code": "1"}).observations
    assert zero[0]["value"] == "0"


def test_sidra_rejects_wrong_unit_or_classification_category() -> None:
    body = [{"header": "value"}, {"D1C": "1", "D3C": "202607", "V": "2", "MC": "31", "D4C": "129314"}]
    assert auditor._sidra_rows(body, {"territory_code": "1", "unit_code": "30", "category_id": 129314}).observations == []
    assert auditor._sidra_rows(body, {"territory_code": "1", "unit_code": "31", "category_id": 999}).observations == []


def test_sidra_header_and_bad_shape_are_not_observations() -> None:
    with pytest.raises(ValueError, match="invalid_source_shape"):
        auditor._sidra_rows([], {"territory_code": "1"})


def test_sidra_probe_falls_back_to_last_five_when_latest_is_only_suppressed(monkeypatch) -> None:
    spec = next(iter(SIDRA_DATASETS.values()))
    monkeypatch.setattr(auditor, "SIDRA_DATASETS", {spec.key: spec})
    latest = [{"header": "V"}, {"D1C": "1", "D2C": str(spec.variable_id), "D3C": "202610", "V": "...", "MC": spec.unit_code, "D4C": str(spec.category_id)}]
    fallback = [
        {"header": "V"},
        {"D1C": "1", "D2C": str(spec.variable_id), "D3C": "202610", "V": "...", "MC": spec.unit_code, "D4C": str(spec.category_id)},
        {"D1C": "1", "D2C": str(spec.variable_id), "D3C": "202609", "V": "0", "MC": spec.unit_code, "D4C": str(spec.category_id)},
    ]
    with patch.object(auditor, "_json", side_effect=[latest, fallback]) as fetch:
        result = auditor._probe_sidra({"sidra": []}, "checked")
    assert fetch.call_count == 2
    assert result[0]["status"] == "invalid_source"
    assert result[0]["error_code"] == "newer_sidra_observation_unavailable"
    assert result[0]["source_reference"] == "202609"
    assert result[0]["source_latest_period"] == "202610"
    assert result[0]["source_unavailable_newer_rows"] == 1


def test_sidra_selects_latest_valid_period_only_after_scope_filters_and_marks_newer_placeholders() -> None:
    expected = {"territory_code": "1", "variable_id": 10, "unit_code": "30", "category_id": 5}
    payload = [
        {"header": "value"},
        {"D1C": "1", "D2C": "10", "D3C": "202607", "D4C": "5", "MC": "30", "V": "3.5"},
        {"D1C": "1", "D2C": "10", "D3C": "202608", "D4C": "5", "MC": "30", "V": "..."},
        {"D1C": "1", "D2C": "10", "D3C": "202609", "D4C": "5", "MC": "30", "V": "X"},
        {"D1C": "2", "D2C": "10", "D3C": "202612", "D4C": "5", "MC": "30", "V": "99"},
        {"D1C": "1", "D2C": "10", "D3C": "202611", "D4C": "999", "MC": "30", "V": "98"},
        {"D1C": "1", "D2C": "999", "D3C": "202610", "D4C": "5", "MC": "30", "V": "97"},
    ]
    selection = auditor._sidra_rows(payload, expected)
    rows = selection.observations
    assert len(rows) == 1
    assert rows[0]["reference_period"] == "202607"
    assert rows[0]["value"] == "3.5"
    assert rows[0]["latest_source_period"] == "202609"
    assert rows[0]["unavailable_newer_rows"] == 2


def test_sidra_latest_dash_is_a_valid_zero_and_not_an_older_period() -> None:
    expected = {"territory_code": "1", "variable_id": 10, "unit_code": "30", "category_id": 5}
    rows = auditor._sidra_rows([
        {"D1C": "1", "D2C": "10", "D3C": "202607", "D4C": "5", "MC": "30", "V": "3.5"},
        {"D1C": "1", "D2C": "10", "D3C": "202608", "D4C": "5", "MC": "30", "V": "-"},
    ], expected).observations
    assert len(rows) == 1
    assert rows[0]["reference_period"] == "202608"
    assert rows[0]["value"] == "0"
    assert rows[0]["unavailable_newer_rows"] == 0


def test_sidra_newer_placeholder_keeps_older_comparison_but_cannot_pass_healthy(monkeypatch) -> None:
    spec = next(iter(SIDRA_DATASETS.values()))
    monkeypatch.setattr(auditor, "SIDRA_DATASETS", {spec.key: spec})
    latest = [{"header": "V"}, {
        "D1C": "1", "D2C": str(spec.variable_id), "D3C": "202610",
        "V": "...", "MC": spec.unit_code, "D4C": str(spec.category_id),
    }]
    recent = [
        {"header": "V"},
        {"D1C": "1", "D2C": str(spec.variable_id), "D3C": "202610", "V": "...", "MC": spec.unit_code, "D4C": str(spec.category_id)},
        {"D1C": "1", "D2C": str(spec.variable_id), "D3C": "202609", "V": "X", "MC": spec.unit_code, "D4C": str(spec.category_id)},
        {"D1C": "1", "D2C": str(spec.variable_id), "D3C": "202608", "V": "4.2", "MC": spec.unit_code, "D4C": str(spec.category_id)},
    ]
    db = [{"dataset_key": spec.key, "reference_period": "202608", "value": 4.1, "unit": spec.unit, "available_at": "db-time"}]
    with patch.object(auditor, "_json", side_effect=[latest, recent]):
        result = auditor._probe_sidra({"sidra": db}, "checked")[0]
    assert result["source_reference"] == "202608"
    assert result["source_value"] == "4.2"
    assert result["database_value"] == 4.1
    assert result["database_reference"] == "202608"
    assert result["status"] == "invalid_source"
    assert result["error_code"] == "newer_sidra_observation_unavailable"
    assert result["source_latest_period"] == "202610"
    assert result["source_unavailable_rows"] == 2
    assert result["source_unavailable_newer_rows"] == 2


def test_sidra_response_with_only_out_of_scope_rows_is_invalid_not_healthy(monkeypatch) -> None:
    spec = next(iter(SIDRA_DATASETS.values()))
    monkeypatch.setattr(auditor, "SIDRA_DATASETS", {spec.key: spec})
    wrong_territory = [{"header": "V"}, {
        "D1C": "2", "D2C": str(spec.variable_id), "D3C": "202610",
        "V": "9", "MC": spec.unit_code, "D4C": str(spec.category_id),
    }]
    with patch.object(auditor, "_json", side_effect=[wrong_territory, wrong_territory]):
        result = auditor._probe_sidra({"sidra": []}, "checked")[0]
    assert result["status"] == "invalid_source"
    assert result["error_code"] == "no_valid_sidra_observation"


def test_focus_uses_latest_records_by_target_dimension_and_vintages() -> None:
    source_records = [
        {"Indicador": "IPCA", "Data": "2026-10-20", "DataReferencia": "2027", "baseCalculo": 0, "Suavizada": "S", "Mediana": 4.1},
        {"Indicador": "IPCA", "Data": "2026-10-20", "DataReferencia": "2028", "baseCalculo": 0, "Suavizada": "S", "Mediana": 3.9},
    ]
    saved = [
        {"series_key": "IPCA", "available_at": "2026-10-20T12:00:00Z", "raw_value_json": row}
        for row in source_records
    ]
    with patch.object(auditor, "_json", return_value={"value": source_records}):
        result = auditor._probe_focus({"focus_vintages": saved}, "2026-10-24T12:00:00Z")
    annual = [row for row in result if row["identity"].startswith("focus:IPCA:")]
    assert len(annual) == 2
    assert {row["status"] for row in annual} == {"matched"}


def test_focus_12m_requires_exact_filters_and_flags_missing_database_rows() -> None:
    focus_rows = []

    def fake_json(url, params):
        if "Inflacao12Meses" in url:
            assert params["$filter"] == "Indicador eq 'IPCA' and Suavizada eq 'S' and baseCalculo eq 0"
            return {"value": [{"Indicador": "IPCA", "Data": "2026-10-20", "DataReferencia": "2027-10", "baseCalculo": 0, "Suavizada": "S", "Mediana": 4.0}]}
        indicator = next(key for key, value in auditor.FOCUS_ANNUAL.items() if value in params["$filter"])
        return {"value": [{"Indicador": auditor.FOCUS_ANNUAL[indicator], "Data": "2026-10-20", "DataReferencia": "2027", "baseCalculo": 0, "Suavizada": "S", "Mediana": 4.0}]}

    with patch.object(auditor, "_json", side_effect=fake_json):
        results = auditor._probe_focus({"focus_vintages": focus_rows}, "2026-10-24T12:00:00Z")
    focus12 = next(item for item in results if item["identity"].startswith("focus:IPCA_12M"))
    assert focus12["status"] == "database_missing"


def test_focus_invalid_json_is_reported_per_indicator_not_hidden() -> None:
    with patch.object(auditor, "_json", return_value={"wrong": []}):
        results = auditor._probe_focus({"focus_vintages": []}, "checked")
    assert len(results) == 6
    assert all(row["status"] == "source_unavailable" for row in results)


def test_anbima_independent_workbook_probe_compares_every_numeric_series(monkeypatch) -> None:
    class Worksheet:
        def to_python(self):
            return [
                ["Data de Referência", "IMA-B", "IMA-B 5"],
                ["2026-10-23", 100.25, 99.5],
                ["2026-10-24", 100.5, 99.75],
            ]

    class Workbook:
        def __init__(self):
            self.sheet_names = ["IMA-B"]

        def get_sheet_by_name(self, _name):
            return Worksheet()

    class Calamine:
        @staticmethod
        def from_filelike(_stream):
            return Workbook()

    monkeypatch.setitem(__import__("sys").modules, "python_calamine", types.SimpleNamespace(CalamineWorkbook=Calamine))
    monkeypatch.setattr(auditor, "_get", lambda *_args, **_kwargs: b"bounded-xls-content")
    database = {"anbima": [
        {"sheet_name": "ima_b", "row_index": 1, "date": "2026-10-24", "fetched_at": "db-time",
         "data_json": {"Data de Referência": "2026-10-24", "IMA-B": 100.5, "IMA-B 5": 99.75}},
    ]}
    results = auditor._probe_anbima(database, "checked")
    assert len(results) == 2
    assert {row["status"] for row in results} == {"matched"}
    assert {row["identity"] for row in results} == {"anbima:IMA-B:IMA-B", "anbima:IMA-B:IMA-B 5"}


def test_anbima_unsupported_xls_parser_returns_error_not_success(monkeypatch) -> None:
    monkeypatch.setattr(auditor, "_get", lambda *_args, **_kwargs: b"bad workbook")
    class BrokenCalamine:
        @staticmethod
        def from_filelike(_stream):
            raise ValueError("bad xls")
    monkeypatch.setitem(__import__("sys").modules, "python_calamine", types.SimpleNamespace(CalamineWorkbook=BrokenCalamine))
    result = auditor._probe_anbima({}, "checked")
    assert result == [{"identity": "anbima:IMA-B", "status": "source_unavailable", "error_code": "ValueError", "source_checked_at": "checked"}]


def test_treasury_probe_compares_all_published_prices_and_yields(monkeypatch) -> None:
    today = datetime.now(timezone.utc).date()
    maturity = today.replace(year=today.year + 5).strftime("%d/%m/%Y")
    base_date = today.strftime("%d/%m/%Y")
    db_base = today.isoformat()
    csv_bytes = (
        b"Tipo Titulo;Data Vencimento;Data Base;Taxa Compra Manha;Taxa Venda Manha;PU Compra Manha;PU Venda Manha;PU Base Manha\n"
        + f"Tesouro Prefixado;{maturity};{base_date};11,1;11,2;900,1;901,2;899,0\n".encode()
        + f"Tesouro Prefixado;01/01/2020;{base_date};11,1;11,2;900,1;901,2;899,0\n".encode()
    )
    saved = {
        "name": "Tesouro Prefixado", "maturityDate": maturity,
        "_db_base_date": db_base, "_db_fetched_at": "db-time",
        "buyRate": 11.1, "sellRate": 11.2, "buyPrice": 900.1,
        "sellPrice": 901.2, "basePrice": 899.0,
    }
    monkeypatch.setattr(auditor, "_get", lambda *_args, **_kwargs: csv_bytes)
    result = auditor._probe_treasury({"treasury": [{"data_json": saved, "base_date": db_base, "fetched_at": "db-time"}]}, "checked")
    assert len(result) == 5
    assert all(row["status"] == "matched" for row in result)
    assert {row["identity"].rsplit(":", 1)[-1] for row in result} == {
        "buy_rate", "sell_rate", "buy_price", "sell_price", "base_price",
    }


def test_treasury_probe_marks_new_active_instrument_database_missing(monkeypatch) -> None:
    today = datetime.now(timezone.utc).date()
    maturity = today.replace(year=today.year + 5).strftime("%d/%m/%Y")
    base_date = today.strftime("%d/%m/%Y")
    body = f"Tipo Titulo;Data Vencimento;Data Base;Taxa Venda Manha\nTesouro Prefixado;{maturity};{base_date};11,2\n".encode()
    monkeypatch.setattr(auditor, "_get", lambda *_args, **_kwargs: body)
    result = auditor._probe_treasury({"treasury": []}, "checked")
    assert result[0]["status"] == "database_missing"
    assert result[0]["identity"].endswith(":buy_rate")


def test_sgs_probe_visits_all_catalog_series_without_production_fetchers() -> None:
    body = [{"data": "01/09/2026", "valor": "0.82"}]
    with patch.object(auditor, "_json", return_value=body) as get_json:
        result = auditor._probe_sgs({"sgs": []}, "2026-10-24T12:00:00Z")
    assert len(result) == len(SGS_CODES) == 64
    assert get_json.call_count == 64
    assert all(item["status"] == "database_missing" for item in result)


def test_treasury_csv_latest_per_bond_and_locale_safe_numeric_values() -> None:
    raw = (
        b"Tipo Titulo;Data Vencimento;Data Base;Taxa Venda Manha\n"
        b"Tesouro Prefixado;01/01/2029;23/10/2026;12,34\n"
        b"Tesouro Prefixado;01/01/2029;24/10/2026;12,50\n"
    )
    result = auditor._parse_treasury_csv(raw)
    assert result[("Tesouro Prefixado", "01/01/2029")]["Data Base"] == "24/10/2026"
    assert auditor._number(result[("Tesouro Prefixado", "01/01/2029")]["Taxa Venda Manha"]) == auditor.Decimal("12.50")


def test_b3_parser_rejects_dtd_and_extracts_official_symbol_rate() -> None:
    identity = {"DI1F30": {"maturity": "2030-01-02", "security_id": "ID-1"}}
    xml = b'''<Document xmlns="urn:bvmf.217.01.xsd"><PricRpt><SctyId><TckrSymb>DI1F30</TckrSymb></SctyId><FinInstrmAttrbts><AdjstdQtTax>12.345</AdjstdQtTax></FinInstrmAttrbts></PricRpt></Document>'''
    assert auditor._parse_b3_xml(xml, identity) == [{"symbol": "DI1F30", "rate": "12.345", "maturity": "2030-01-02", "security_id": None, "trade_date": None}]
    with pytest.raises(ValueError, match="dtd_forbidden"):
        auditor._parse_b3_xml(b"<!DOCTYPE x [<!ENTITY a 'b'>]><x/>", identity)


def test_b3_probe_compares_latest_curve_and_flags_active_missing_contract(monkeypatch) -> None:
    import source_audit.auditor as implementation

    today = datetime.now(timezone.utc).date()
    trade_day = next(today - timedelta(days=offset) for offset in range(5) if (today - timedelta(days=offset)).weekday() < 5)
    trade_date = trade_day.isoformat()
    symbol = "DI1F99"
    identity = {"snapshot_date": trade_date, "report_date": trade_date, "symbol": symbol,
                "security_id": "SEC-99", "maturity": today.replace(year=today.year + 5).isoformat(),
                "trading_start": trade_date, "trading_end": None}
    xml = f'''<Document xmlns="{implementation._B3_NS}"><PricRpt><TradDt><Dt>{trade_date}</Dt></TradDt><SctyId><TckrSymb>{symbol}</TckrSymb></SctyId><FinInstrmId><OthrId><Id>SEC-99</Id></OthrId></FinInstrmId><FinInstrmAttrbts><AdjstdQtTax>12.345</AdjstdQtTax></FinInstrmAttrbts></PricRpt></Document>'''.encode()
    archive = io.BytesIO()
    with zipfile.ZipFile(archive, "w") as zipped:
        zipped.writestr("quotes.xml", xml)
    monkeypatch.setattr(implementation, "_get", lambda *_args, **_kwargs: archive.getvalue())
    monkeypatch.setattr(implementation.time, "sleep", lambda _seconds: None)
    database = {
        "b3_identity": [identity],
        "b3_di": [{"symbol": symbol, "trade_date": trade_date, "maturity": identity["maturity"], "rate": 12.345, "fetched_at": "db-time"},
                   {"symbol": "DI1F98", "trade_date": trade_date, "maturity": identity["maturity"], "rate": 12.0}],
    }
    result = implementation._probe_b3(database, "checked")
    assert next(row for row in result if row["identity"].startswith("b3_di:DI1F99"))["status"] == "matched"
    assert next(row for row in result if row["identity"].startswith("b3_di:DI1F98"))["error_code"] == "active_contract_missing_from_latest_curve"


def test_independent_sgs_adapter_does_not_call_ingestion_parser_or_cache(monkeypatch) -> None:
    import datafetchers.bcb_sgs as production

    monkeypatch.setattr(production, "fetch_sgs_batch", lambda *_args, **_kwargs: pytest.fail("ingestion called"))
    monkeypatch.setattr(production, "_parse_sgs_payload", lambda *_args, **_kwargs: pytest.fail("ingestion parser called"), raising=False)
    monkeypatch.setattr(auditor, "_json", lambda *_args, **_kwargs: [{"data": "23/10/2026", "valor": "0"}])
    result = auditor._probe_sgs({"sgs": []}, "checked")
    assert len(result) == 64
    assert all(row["status"] == "database_missing" for row in result)


def test_b3_archive_checks_uncompressed_size_before_reading_members() -> None:
    archive = io.BytesIO()
    with zipfile.ZipFile(archive, "w") as zipped:
        zipped.writestr("bad.xml", b"<x/>")
    with patch.object(auditor, "MAX_BINARY_BYTES", 1), pytest.raises(ValueError, match="archive_member_limit"):
        auditor._parse_b3(archive.getvalue(), {})


def test_report_markdown_never_includes_environment_secrets() -> None:
    report = {
        "status": "degraded", "generated_at": "now", "database_snapshot_at": "then",
        "coverage": {"checked": 0, "complete": False}, "sources": {},
    }
    text = auditor.render_markdown(report)
    assert "Authorization" not in text and "Bearer" not in text
    assert "auditoria" in text


def test_report_degraded_if_any_family_probe_fails(monkeypatch) -> None:
    monkeypatch.setattr(auditor, "_probe_sgs", lambda *_: [{"identity": "s", "status": "source_unavailable"}])
    monkeypatch.setattr(auditor, "_probe_focus", lambda *_: [{"identity": "f", "status": "matched"}] * 6)
    monkeypatch.setattr(auditor, "_probe_sidra", lambda *_: [{"identity": "i", "status": "matched"}] * 5)
    monkeypatch.setattr(auditor, "_probe_b3", lambda *_: [{"identity": "b", "status": "matched"}])
    monkeypatch.setattr(auditor, "_probe_anbima", lambda *_: [{"identity": "a", "status": "matched"}])
    monkeypatch.setattr(auditor, "_probe_treasury", lambda *_: [{"identity": "t", "status": "matched"}])
    report = auditor.run_audit({"database": {}}, clock=datetime(2026, 10, 24, tzinfo=timezone.utc))
    assert report["status"] == "degraded"
    assert report["coverage"]["expected_static"] == 75


def test_complete_source_inventory_is_healthy_only_when_all_dynamic_sources_match(monkeypatch) -> None:
    monkeypatch.setattr(auditor, "_probe_sgs", lambda *_: [{"identity": f"bcb_sgs:{code}", "status": "matched"} for code in SGS_CODES])
    monkeypatch.setattr(auditor, "_probe_focus", lambda *_: [{"identity": f"focus:{key}:2027", "status": "matched"} for key in (*auditor.FOCUS_ANNUAL, auditor.FOCUS_12M)])
    monkeypatch.setattr(auditor, "_probe_sidra", lambda *_: [{"identity": f"ibge_sidra:{spec.key}", "status": "matched"} for spec in SIDRA_DATASETS.values()])
    monkeypatch.setattr(auditor, "_probe_b3", lambda *_: [{"identity": "b3_di:DI1", "status": "matched"}])
    monkeypatch.setattr(auditor, "_probe_anbima", lambda *_: [{"identity": "anbima:IMA-B:IMA-B", "status": "matched"}])
    monkeypatch.setattr(auditor, "_probe_treasury", lambda *_: [{"identity": "tesouro:Tesouro Prefixado:2030:buy_rate", "status": "matched"}])
    report = auditor.run_audit({"database": {"audit_limits": [{"focus_omitted": 0, "anbima_omitted": 0}]}})
    assert report["status"] == "healthy"
    assert report["coverage"]["complete"] is True
    assert report["coverage"]["counts_by_source"] == {
        "sgs": 64, "focus": 6, "sidra": 5, "b3_di": 1, "anbima": 1, "treasury": 1,
    }


@pytest.mark.parametrize("failed_family", ["sgs", "focus", "sidra", "b3_di", "anbima", "treasury"])
def test_any_provider_failure_prevents_a_healthy_result(monkeypatch, failed_family) -> None:
    expected = {
        "sgs": [{"identity": f"bcb_sgs:{code}", "status": "matched"} for code in SGS_CODES],
        "focus": [{"identity": f"focus:{key}:2027", "status": "matched"} for key in (*auditor.FOCUS_ANNUAL, auditor.FOCUS_12M)],
        "sidra": [{"identity": f"ibge_sidra:{spec.key}", "status": "matched"} for spec in SIDRA_DATASETS.values()],
        "b3_di": [{"identity": "b3_di:DI1", "status": "matched"}],
        "anbima": [{"identity": "anbima:IMA-B:IMA-B", "status": "matched"}],
        "treasury": [{"identity": "tesouro:bond:rate", "status": "matched"}],
    }
    methods = {"sgs": "_probe_sgs", "focus": "_probe_focus", "sidra": "_probe_sidra",
               "b3_di": "_probe_b3", "anbima": "_probe_anbima", "treasury": "_probe_treasury"}
    for family, method in methods.items():
        monkeypatch.setattr(auditor, method, lambda *_args, family=family: expected[family])
    monkeypatch.setattr(auditor, methods[failed_family], lambda *_: [{"identity": f"{failed_family}:test", "status": "source_unavailable", "error_code": "TimeoutException"}])
    report = auditor.run_audit({"database": {}})
    assert report["status"] == "degraded"
    assert report["counts_by_status"]["source_unavailable"] == 1


def test_run_audit_requires_database_snapshot_shape() -> None:
    with pytest.raises(TypeError, match="invalid_database_snapshot"):
        auditor.run_audit({"captured_at": "not-a-database"})


def test_database_snapshot_cap_is_reported_as_incomplete_not_healthy(monkeypatch) -> None:
    monkeypatch.setattr(auditor, "_probe_sgs", lambda *_: [{"identity": f"bcb_sgs:{code}", "status": "matched"} for code in SGS_CODES])
    monkeypatch.setattr(auditor, "_probe_focus", lambda *_: [{"identity": f"focus:{key}:dimension", "status": "matched"} for key in (*auditor.FOCUS_ANNUAL, auditor.FOCUS_12M)])
    monkeypatch.setattr(auditor, "_probe_sidra", lambda *_: [{"identity": f"ibge_sidra:{spec.key}", "status": "matched"} for spec in SIDRA_DATASETS.values()])
    monkeypatch.setattr(auditor, "_probe_b3", lambda *_: [{"identity": "b3_di:DI1", "status": "matched"}])
    monkeypatch.setattr(auditor, "_probe_anbima", lambda *_: [{"identity": "anbima:IMA-B:series", "status": "matched"}])
    monkeypatch.setattr(auditor, "_probe_treasury", lambda *_: [{"identity": "tesouro:bond:rate", "status": "matched"}])
    result = auditor.run_audit({"database": {"audit_limits": [{"focus_omitted": 1, "anbima_omitted": 0}]}})
    assert result["status"] == "degraded"
    assert result["coverage"]["complete"] is False
    assert result["coverage"]["database_limit_exceeded"] is True


def test_snapshot_query_reads_consistent_latest_slices_from_sqlite(tmp_path, monkeypatch) -> None:
    from db import store

    monkeypatch.setattr(store, "DB_PATH", tmp_path / "audit.db")
    monkeypatch.setattr(store, "_conn", None)
    store.init_db()
    store.upsert_sgs(433, [{"data": "01/09/2026", "valor": "0.82"}])
    store.upsert_focus("IPCA", [{"Data": "2026-10-20", "DataReferencia": "2027", "Mediana": 4.1}])
    store.upsert_anbima("ima_b", [{"Data de Referência": "2026-10-20", "IMA-B": 100.1}])
    result = store.source_audit_rows()
    assert result["sgs"][0]["date"] == "2026-09-01"
    assert result["focus_vintages"][0]["provider"] == "focus"
    assert result["anbima"][0]["sheet_name"] == "ima_b"


def test_snapshot_exposes_overflow_counts_instead_of_calling_truncated_coverage_complete(tmp_path, monkeypatch) -> None:
    from db import store

    monkeypatch.setattr(store, "DB_PATH", tmp_path / "limited-audit.db")
    monkeypatch.setattr(store, "_conn", None)
    store.init_db()
    store.upsert_focus("IPCA", [
        {"Data": f"2026-{month:02d}-01", "DataReferencia": "2027", "Mediana": month}
        for month in range(1, 13)
    ])
    store.upsert_anbima("ima_b", [{"value": index} for index in range(501)])
    snapshot = store.source_audit_rows()
    assert snapshot["audit_limits"] == [{"focus_omitted": 0, "anbima_omitted": 1}]
    assert len(snapshot["anbima"]) == 500


def test_snapshot_endpoint_requires_existing_refresh_token_and_never_writes(monkeypatch) -> None:
    from fastapi.testclient import TestClient

    from db import store
    from main import app

    monkeypatch.setenv("XBRAY_ENV", "production")
    monkeypatch.setenv("XBRAY_REFRESH_TOKEN", "test-token")
    monkeypatch.setattr(store, "source_audit_rows", lambda: {"sgs": []})
    with TestClient(app) as client:
        denied = client.get("/api/source-audit/snapshot")
        assert denied.status_code == 401
    accepted = client.get("/api/source-audit/snapshot", headers={"Authorization": f"Bearer {'test'}-token"})
    assert accepted.status_code == 200
    assert accepted.json()["schema_version"] == "source-audit-snapshot-v1"
    assert accepted.json()["database"]["sgs"] == []


def test_snapshot_endpoint_missing_token_is_503_and_wrong_token_is_401(monkeypatch) -> None:
    from fastapi.testclient import TestClient

    from db import store
    from main import app

    monkeypatch.setenv("XBRAY_ENV", "production")
    monkeypatch.delenv("XBRAY_REFRESH_TOKEN", raising=False)
    monkeypatch.setattr(store, "source_audit_rows", lambda: pytest.fail("auth must reject before reading database"))
    with TestClient(app) as client:
        unconfigured = client.get("/api/source-audit/snapshot")
        monkeypatch.setenv("XBRAY_REFRESH_TOKEN", "expected")
        unauthorized = client.get("/api/source-audit/snapshot", headers={"Authorization": "Bearer wrong"})
        malformed = client.get("/api/source-audit/snapshot", headers={"Authorization": "Basic wrong"})
    assert unconfigured.status_code == 503
    assert unauthorized.status_code == 401
    assert malformed.status_code == 401


def test_snapshot_endpoint_empty_snapshot_includes_fixed_inventory(monkeypatch) -> None:
    from fastapi.testclient import TestClient

    from db import store
    from main import app

    monkeypatch.setenv("XBRAY_ENV", "production")
    monkeypatch.setenv("XBRAY_REFRESH_TOKEN", "token")
    monkeypatch.setattr(store, "source_audit_rows", dict)
    with TestClient(app) as client:
        response = client.get("/api/source-audit/snapshot", headers={"Authorization": "Bearer token"})
    body = response.json()
    assert response.status_code == 200
    assert len(body["inventory"]["sgs"]) == 64
    assert len(body["inventory"]["focus_annual"]) == 5
    assert len(body["inventory"]["sidra"]) == 5


def test_snapshot_decode_marks_corrupt_json_instead_of_silently_treating_as_data() -> None:
    from routes.source_audit import _decode_rows

    decoded = _decode_rows({
        "focus": [{"data_json": "not-json"}],
        "anbima": [{"data_json": "{\"x\": 1}"}],
        "treasury": [{"data_json": "null"}],
        "focus_vintages": [{"raw_value_json": "[]"}, {"raw_value_json": "bad"}],
        "sgs": [{"value": 1}],
    })
    assert decoded["focus"][0] == {"data_json": None, "decode_error": True}
    assert decoded["anbima"][0]["data_json"] == {"x": 1}
    assert decoded["treasury"][0]["data_json"] is None
    assert decoded["focus_vintages"][0]["raw_value_json"] == []
    assert decoded["focus_vintages"][1]["decode_error"] is True
    assert decoded["sgs"][0]["value"] == 1


def test_snapshot_decode_preserves_postgres_native_jsonb_objects() -> None:
    from routes.source_audit import _decode_rows

    decoded = _decode_rows({
        "focus": [{"data_json": {"Indicador": "IPCA", "Mediana": 4.1}}],
        "focus_vintages": [{"raw_value_json": {"Indicador": "IPCA"}}],
    })
    assert decoded["focus"][0]["data_json"] == {"Indicador": "IPCA", "Mediana": 4.1}
    assert decoded["focus_vintages"][0]["raw_value_json"] == {"Indicador": "IPCA"}


def test_cli_writes_json_markdown_and_summary_then_exits_degraded(tmp_path, monkeypatch) -> None:
    from source_audit import cli

    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(cli, "REPORT_JSON", tmp_path / "report.json")
    monkeypatch.setattr(cli, "REPORT_MARKDOWN", tmp_path / "report.md")
    summary = tmp_path / "summary.md"
    monkeypatch.setenv("GITHUB_STEP_SUMMARY", str(summary))
    monkeypatch.setattr(cli, "_snapshot", lambda: {"schema_version": "source-audit-snapshot-v1", "database": {}})
    monkeypatch.setattr(cli, "run_audit", lambda _snapshot: {
        "schema_version": "source-audit-report-v1", "status": "degraded", "generated_at": "now",
        "database_snapshot_at": "then", "coverage": {"checked": 1, "complete": False},
        "sources": {"sgs": [{"identity": "bcb_sgs:1", "status": "source_unavailable"}]},
    })
    assert cli.main() == 1
    report = __import__("json").loads((tmp_path / "report.json").read_text())
    assert report["status"] == "degraded"
    assert "source_unavailable" in (tmp_path / "report.md").read_text()
    assert summary.read_text() == (tmp_path / "report.md").read_text()


def test_cli_global_failure_emits_safe_degraded_artifact(tmp_path, monkeypatch) -> None:
    from source_audit import cli

    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(cli, "REPORT_JSON", tmp_path / "report.json")
    monkeypatch.setattr(cli, "REPORT_MARKDOWN", tmp_path / "report.md")
    monkeypatch.setattr(cli, "_snapshot", lambda: (_ for _ in ()).throw(RuntimeError("do-not-expose-secret")))
    assert cli.main() == 1
    output = (tmp_path / "report.json").read_text()
    assert "RuntimeError" in output
    assert "do-not-expose-secret" not in output


def test_cli_snapshot_requests_only_authenticated_read_only_endpoint(monkeypatch) -> None:
    from source_audit import cli

    observed: dict[str, object] = {}

    class Response:
        def raise_for_status(self):
            return None

        @staticmethod
        def json():
            return {"schema_version": "source-audit-snapshot-v1", "database": {}}

    class Client:
        def __init__(self, **kwargs):
            observed["timeout"] = kwargs["timeout"]
            observed["follow_redirects"] = kwargs["follow_redirects"]

        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return None

        def get(self, url, *, headers):
            observed["url"] = url
            observed["headers"] = headers
            return Response()

    monkeypatch.setenv("XBRAY_API_URL", "https://production.example")
    monkeypatch.setenv("XBRAY_REFRESH_TOKEN", "private-test-token")
    monkeypatch.setattr(cli.httpx, "Client", Client)
    snapshot = cli._snapshot()
    assert snapshot["schema_version"] == "source-audit-snapshot-v1"
    assert observed["url"] == "https://production.example/api/source-audit/snapshot"
    assert observed["follow_redirects"] is False
    assert observed["headers"] == {"Authorization": "Bearer private-test-token", "Accept": "application/json"}


def test_cli_snapshot_rejects_missing_configuration_and_wrong_schema(monkeypatch) -> None:
    from source_audit import cli

    monkeypatch.delenv("XBRAY_API_URL", raising=False)
    monkeypatch.delenv("XBRAY_REFRESH_TOKEN", raising=False)
    monkeypatch.setattr(cli, "SNAPSHOT_PATH", None)
    with pytest.raises(RuntimeError, match="configuration_missing"):
        cli._snapshot()
    monkeypatch.setattr(cli, "SNAPSHOT_PATH", None)

    class Response:
        @staticmethod
        def raise_for_status():
            return None

        @staticmethod
        def json():
            return {"schema_version": "wrong"}

    class Client:
        def __init__(self, **_kwargs):
            pass

        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return None

        def get(self, *_args, **_kwargs):
            return Response()

    monkeypatch.setenv("XBRAY_API_URL", "https://production.example")
    monkeypatch.setenv("XBRAY_REFRESH_TOKEN", "token")
    monkeypatch.setattr(cli.httpx, "Client", Client)
    with pytest.raises(TypeError, match="invalid_source_audit_snapshot"):
        cli._snapshot()


def test_cli_snapshot_file_validates_schema_and_reports_bad_json(tmp_path, monkeypatch) -> None:
    from source_audit import cli

    snapshot = tmp_path / "snapshot.json"
    monkeypatch.setattr(cli, "SNAPSHOT_PATH", snapshot)
    snapshot.write_text('{"schema_version":"source-audit-snapshot-v1","database":{}}', encoding="utf-8")
    assert cli._snapshot()["schema_version"] == "source-audit-snapshot-v1"
    snapshot.write_text("invalid", encoding="utf-8")
    with pytest.raises(ValueError):
        cli._snapshot()


def test_cli_healthy_audit_exits_zero(tmp_path, monkeypatch) -> None:
    from source_audit import cli

    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(cli, "REPORT_JSON", tmp_path / "report.json")
    monkeypatch.setattr(cli, "REPORT_MARKDOWN", tmp_path / "report.md")
    monkeypatch.delenv("GITHUB_STEP_SUMMARY", raising=False)
    monkeypatch.setattr(cli, "_snapshot", lambda: {"database": {}})
    monkeypatch.setattr(cli, "run_audit", lambda _snapshot: {
        "status": "healthy", "generated_at": "now", "database_snapshot_at": "then",
        "coverage": {"checked": 75, "complete": True}, "sources": {},
    })
    assert cli.main() == 0
    assert (tmp_path / "report.json").exists()


def test_weekly_workflow_has_separate_permissions_artifacts_and_issue_lifecycle() -> None:
    from pathlib import Path

    workflow = (Path(__file__).parents[2] / ".github/workflows/source-audit-weekly.yml").read_text()
    assert 'cron: "0 12 * * 1"' in workflow
    assert "workflow_dispatch:" in workflow
    assert "issues: write" in workflow and "contents: read" in workflow
    assert "retention-days: 90" in workflow
    assert "XBRAY_API_URL" in workflow and "XBRAY_REFRESH_TOKEN" in workflow
    assert "state_reason: 'completed'" in workflow
    assert "github.paginate(github.rest.issues.listForRepo" in workflow
    assert "if-no-files-found: warn" in workflow
    assert "/api/refresh" not in workflow
