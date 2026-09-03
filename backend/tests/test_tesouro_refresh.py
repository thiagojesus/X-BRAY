from unittest.mock import MagicMock, patch

import pytest

import datafetchers.tesouro_direto as tesouro


@pytest.fixture(autouse=True)
def _reset_csv_cache():
    tesouro._CSV_CACHE = None
    yield
    tesouro._CSV_CACHE = None


def test_load_csv_rows_reuses_memory_cache() -> None:
    # Given
    cached = [{"Tipo Titulo": "Tesouro Prefixado"}]
    tesouro._CSV_CACHE = cached

    # When
    with patch("datafetchers.tesouro_direto.query_treasury_rows") as query_rows:
        result = tesouro._load_csv_rows()

    # Then
    assert result is cached
    query_rows.assert_not_called()


def test_load_csv_rows_returns_none_when_store_is_empty() -> None:
    # Given / When
    with patch("datafetchers.tesouro_direto.query_treasury_rows", return_value=[]):
        result = tesouro._load_csv_rows()

    # Then
    assert result is None


def test_load_csv_rows_populates_memory_cache_from_store() -> None:
    # Given
    stored = [{"Tipo Titulo": "Tesouro IPCA+"}]

    # When
    with patch("datafetchers.tesouro_direto.query_treasury_rows", return_value=stored) as query_rows:
        first = tesouro._load_csv_rows()
        second = tesouro._load_csv_rows()

    # Then
    assert first == stored
    assert second is first
    query_rows.assert_called_once()


def test_download_csv_rows_parses_semicolon_utf8_bom() -> None:
    # Given
    response = MagicMock()
    response.content = (
        "\ufeffTipo Titulo;Data Vencimento;Data Base\n"
        "Tesouro Prefixado;01/01/2030;02/09/2026\n"
    ).encode("utf-8")

    # When
    with patch("datafetchers.tesouro_direto.httpx.get", return_value=response) as provider_get:
        rows = tesouro._download_csv_rows()

    # Then
    response.raise_for_status.assert_called_once()
    assert rows == [{
        "Tipo Titulo": "Tesouro Prefixado",
        "Data Vencimento": "01/01/2030",
        "Data Base": "02/09/2026",
    }]
    assert provider_get.call_args.kwargs["follow_redirects"] is True


def test_refresh_treasury_data_persists_only_identifiable_bonds() -> None:
    # Given
    rows = [
        {"Tipo Titulo": "Tesouro Prefixado", "Data Vencimento": "01/01/2030", "Data Base": "02/09/2026"},
        {"Tipo Titulo": "", "Data Vencimento": "01/01/2030", "Data Base": "02/09/2026"},
        {"Tipo Titulo": "Tesouro IPCA+", "Data Vencimento": "", "Data Base": "02/09/2026"},
    ]

    # When
    with (
        patch("datafetchers.tesouro_direto._download_csv_rows", return_value=rows),
        patch("datafetchers.tesouro_direto.replace_treasury_rows", return_value=1) as replace_rows,
        patch("datafetchers.tesouro_direto.set_meta") as set_meta,
    ):
        result = tesouro.refresh_treasury_data()

    # Then
    persisted = replace_rows.call_args.args[0]
    assert len(persisted) == 1
    assert isinstance(persisted[0]["_bond_code"], int)
    assert result == {"rows": 1}
    set_meta.assert_called_once()


def test_refresh_treasury_data_rejects_empty_replacement() -> None:
    # Given / When / Then
    with (
        patch("datafetchers.tesouro_direto._download_csv_rows", return_value=[]),
        patch("datafetchers.tesouro_direto.replace_treasury_rows", return_value=0),
        pytest.raises(RuntimeError, match="no usable rows"),
    ):
        tesouro.refresh_treasury_data()


def test_fetch_treasury_quotes_selects_latest_valid_rows() -> None:
    # Given
    rows = [
        {
            "Tipo Titulo": "Tesouro Prefixado",
            "Data Vencimento": "01/01/2030",
            "Data Base": "01/09/2026",
            "Taxa Compra Manha": "11,00",
        },
        {
            "Tipo Titulo": "Tesouro Prefixado",
            "Data Vencimento": "01/01/2030",
            "Data Base": "02/09/2026",
            "Taxa Compra Manha": "12,50",
            "Taxa Venda Manha": "12,40",
            "PU Compra Manha": "900,10",
            "PU Venda Manha": "901,20",
            "PU Base Manha": "899,90",
        },
        {
            "Tipo Titulo": "Tesouro IPCA+ com Juros Semestrais",
            "Data Vencimento": "15/05/2035",
            "Data Base": "02/09/2026",
            "Taxa Compra Manha": "6,20",
            "Taxa Venda Manha": "6,10",
            "PU Compra Manha": "2500,00",
            "PU Venda Manha": "2510,00",
            "PU Base Manha": "2490,00",
        },
        {
            "Tipo Titulo": "CDB",
            "Data Vencimento": "01/01/2030",
            "Data Base": "02/09/2026",
        },
        {
            "Tipo Titulo": "Tesouro Prefixado antigo",
            "Data Vencimento": "01/01/2031",
            "Data Base": "31/08/2026",
        },
        {
            "Tipo Titulo": "Tesouro Prefixado vencido",
            "Data Vencimento": "01/01/2020",
            "Data Base": "02/09/2026",
        },
        {
            "Tipo Titulo": "Tesouro Prefixado inválido",
            "Data Vencimento": "invalid",
            "Data Base": "02/09/2026",
        },
        {
            "Tipo Titulo": "Tesouro Prefixado",
            "Data Vencimento": "",
            "Data Base": "02/09/2026",
        },
        {
            "Tipo Titulo": "Tesouro Prefixado",
            "Data Vencimento": "01/01/2032",
            "Data Base": "invalid",
        },
    ]

    # When
    with patch("datafetchers.tesouro_direto._load_csv_rows", return_value=rows):
        result = tesouro.fetch_treasury_quotes()

    # Then
    assert [bond["indexer"] for bond in result] == ["prefixado", "ipca"]
    assert result[0]["buyRate"] == 12.5
    assert result[0]["basePrice"] == 899.9
    assert result[1]["couponType"] == "semestrais"


def test_fetch_treasury_quotes_returns_empty_for_parser_failure() -> None:
    # Given / When
    with patch("datafetchers.tesouro_direto._load_csv_rows", side_effect=TypeError("invalid rows")):
        result = tesouro.fetch_treasury_quotes()

    # Then
    assert result == []


def test_fetch_yield_curve_handles_empty_quotes() -> None:
    # Given / When
    with patch("datafetchers.tesouro_direto.fetch_treasury_quotes", return_value=[]):
        result = tesouro.fetch_yield_curve()

    # Then
    assert result == {"error": "No data available", "prefixado": [], "ipca": []}


def test_fetch_yield_curve_filters_and_sorts_supported_bonds() -> None:
    # Given
    quotes = [
        {"indexer": "prefixado", "maturityDate": "01/01/2040", "buyRate": 12.0, "durationDays": 5000},
        {"indexer": "ipca", "maturityDate": "01/01/2035", "buyRate": 6.0, "durationDays": 3000},
        {"indexer": "prefixado", "maturityDate": "01/01/2030", "buyRate": 11.0, "durationDays": 1000},
        {"indexer": "prefixado", "maturityDate": "", "buyRate": 10.0, "durationDays": 10},
        {"indexer": "ipca", "maturityDate": "01/01/2031", "buyRate": None, "durationDays": 1500},
        {"indexer": "selic", "maturityDate": "01/01/2032", "buyRate": 1.0, "durationDays": 2000},
    ]

    # When
    with patch("datafetchers.tesouro_direto.fetch_treasury_quotes", return_value=quotes):
        result = tesouro.fetch_yield_curve()

    # Then
    assert [bond["durationDays"] for bond in result["prefixado"]] == [1000, 5000]
    assert [bond["durationDays"] for bond in result["ipca"]] == [3000]


def test_read_wrappers_delegate_to_cached_builders() -> None:
    # Given
    rows = [{"Tipo Titulo": "Tesouro Prefixado"}]

    # When
    with (
        patch("datafetchers.tesouro_direto._load_csv_rows", return_value=rows),
        patch("datafetchers.tesouro_direto.build_bonds_history", return_value=[{"code": 1}]) as build_many,
        patch("datafetchers.tesouro_direto.build_bond_catalog", return_value=[{"code": 1}]) as build_catalog,
        patch("datafetchers.tesouro_direto.build_bond_history", return_value={"code": 1}) as build_one,
    ):
        histories = tesouro.read_bonds_history([1, 2], 45)
        catalog = tesouro.read_bond_catalog()
        history = tesouro.read_bond_history("1", 15)

    # Then
    assert histories == [{"code": 1}]
    assert catalog == [{"code": 1}]
    assert history == {"code": 1}
    build_many.assert_called_once_with(rows, [1, 2], 45)
    build_catalog.assert_called_once_with(rows)
    build_one.assert_called_once_with(rows, 1, 15)
