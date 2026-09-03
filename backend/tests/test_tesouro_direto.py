from datetime import datetime, timedelta
from unittest.mock import patch

from fastapi.testclient import TestClient

from datafetchers import tesouro_direto as td


def _csv_rows() -> list[dict]:
    bonds = [
        ("Tesouro Prefixado", "01/01/2027"),
        ("Tesouro Prefixado", "01/01/2037"),
        ("Tesouro IPCA+", "15/05/2035"),
    ]
    base = datetime(2026, 8, 18)
    rows: list[dict] = []
    for bond_type, maturity in bonds:
        for i in range(4):
            rows.append({
                "Tipo Titulo": bond_type,
                "Data Vencimento": maturity,
                "Data Base": (base - timedelta(days=i)).strftime("%d/%m/%Y"),
                "Taxa Compra Manha": "13.5",
                "Taxa Venda Manha": "13.2",
                "PU Compra Manha": "600",
                "PU Venda Manha": "590",
            })
    return rows


def test_fetch_bonds_history_from_csv_single_pass() -> None:
    codes = [
        td._bond_code("Tesouro Prefixado", "01/01/2027"),
        td._bond_code("Tesouro IPCA+", "15/05/2035"),
    ]
    with patch.object(td, "_load_csv_rows", return_value=_csv_rows()):
        result = td.fetch_bonds_history(codes, days=30)

    assert len(result) == 2
    by_code = {h["code"]: h for h in result}
    assert set(by_code) == set(codes)
    for h in result:
        assert len(h["points"]) == 4
        assert h["points"][0]["buyRate"] == 13.5
        assert h["points"][0]["sellRate"] == 13.2
        assert h["points"][0]["buyPrice"] == 600


def test_fetch_bonds_history_respects_days_window() -> None:
    codes = [td._bond_code("Tesouro Prefixado", "01/01/2027")]
    with patch.object(td, "_load_csv_rows", return_value=_csv_rows()):
        result = td.fetch_bonds_history(codes, days=1)

    assert len(result) == 1
    assert len(result[0]["points"]) == 2  # 18/08 and 17/08 only


def test_fetch_bonds_history_ignores_unknown_codes_without_second_lookup() -> None:
    csv_code = td._bond_code("Tesouro Prefixado", "01/01/2027")
    unknown_code = 157
    with patch.object(td, "_load_csv_rows", return_value=_csv_rows()), \
         patch.object(td, "fetch_bond_history") as single_history:
        result = td.fetch_bonds_history([csv_code, unknown_code], days=30)

    assert [history["code"] for history in result] == [csv_code]
    single_history.assert_not_called()


def test_fetch_bonds_history_empty_codes() -> None:
    with patch.object(td, "_load_csv_rows", return_value=_csv_rows()):
        assert td.fetch_bonds_history([], days=30) == []


def test_quotes_route_uses_cached_csv_without_provider_request() -> None:
    # Given
    from main import app

    # When
    with (
        patch.object(td, "_load_csv_rows", return_value=_csv_rows()),
        patch.object(td.httpx, "get") as provider_get,
        TestClient(app, raise_server_exceptions=False) as client,
    ):
        response = client.get("/api/tesouro-direto")

    # Then
    assert response.status_code == 200
    assert response.json()["data"]["prefixado"]
    assert response.json()["data"]["ipca"]
    provider_get.assert_not_called()


def test_catalog_route_uses_cached_csv_without_blocked_api_request() -> None:
    # Given
    from main import app

    # When
    with (
        patch.object(td, "_load_csv_rows", return_value=_csv_rows()),
        patch.object(td.httpx, "get") as provider_get,
        TestClient(app, raise_server_exceptions=False) as client,
    ):
        response = client.get("/api/tesouro-direto/titulos")

    # Then
    assert response.status_code == 200
    assert response.json()["data"]
    provider_get.assert_not_called()


def test_history_route_uses_cached_csv_without_blocked_api_request() -> None:
    # Given
    from main import app

    code = td._bond_code("Tesouro Prefixado", "01/01/2027")

    # When
    with (
        patch.object(td, "_load_csv_rows", return_value=_csv_rows()),
        patch.object(td.httpx, "get") as provider_get,
        TestClient(app, raise_server_exceptions=False) as client,
    ):
        response = client.get(f"/api/tesouro-direto/historico?code={code}&days=30")

    # Then
    assert response.status_code == 200
    assert response.json()["data"]["points"]
    provider_get.assert_not_called()


def test_missing_treasury_cache_returns_service_unavailable() -> None:
    from main import app

    with (
        patch.object(td, "_load_csv_rows", return_value=None),
        TestClient(app, raise_server_exceptions=False) as client,
    ):
        response = client.get("/api/tesouro-direto")

    assert response.status_code == 503
    assert response.json() == {"error": "dataset_unavailable", "dataset": "tesouro.cotacoes"}
