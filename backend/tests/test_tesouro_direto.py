from datetime import datetime, timedelta
from unittest.mock import patch

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


def test_fetch_bonds_history_falls_back_per_code() -> None:
    csv_code = td._bond_code("Tesouro Prefixado", "01/01/2027")
    json_code = 157  # real treasuryBondCode, absent from the CSV
    fallback = {
        "code": json_code,
        "name": "Tesouro Prefixado",
        "maturityDate": "01/01/2027",
        "points": [{"date": "18/08/2026", "buyRate": 13.9, "sellRate": 13.6, "buyPrice": 610, "sellPrice": 600}],
    }
    with patch.object(td, "_load_csv_rows", return_value=_csv_rows()), \
         patch.object(td, "fetch_bond_history", return_value=fallback) as mock_fetch:
        result = td.fetch_bonds_history([csv_code, json_code], days=30)

    assert len(result) == 2
    mock_fetch.assert_called_once_with(json_code, 30)
    codes = {h["code"] for h in result}
    assert codes == {csv_code, json_code}


def test_fetch_bonds_history_empty_codes() -> None:
    with patch.object(td, "_load_csv_rows", return_value=_csv_rows()):
        assert td.fetch_bonds_history([], days=30) == []