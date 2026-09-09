import csv
from datetime import datetime
from io import StringIO
from typing import NotRequired, TypedDict

import httpx

from datafetchers.tesouro_bonds import (
    ALLOWED_BOND_TYPES,
    _bond_code,
    _get_maturity_label,
    _parse_date,
    _parse_rate,
    build_bond_catalog,
    build_bond_history,
    build_bonds_history,
)
from db.store import (
    query_treasury_latest_rows,
    query_treasury_rows,
    replace_treasury_rows,
    set_meta,
)

TESOURO_TRANSPARENTE_CSV = "https://www.tesourotransparente.gov.br/ckan/dataset/taxas-dos-titulos-ofertados-pelo-tesouro-direto/resource/796d2059-14e9-44e3-80c9-2d9e30b405c1/download"
REQUEST_TIMEOUT = 30


class TreasuryQuote(TypedDict):
    symbol: str
    name: str
    indexer: str
    maturityDate: str
    baseDate: str
    durationDays: int
    maturityLabel: str
    buyRate: float | None
    sellRate: float | None
    buyPrice: float | None
    sellPrice: float | None
    basePrice: float | None
    couponType: str


class YieldCurve(TypedDict):
    prefixado: list[TreasuryQuote]
    ipca: list[TreasuryQuote]
    error: NotRequired[str]


class TreasuryRefreshError(RuntimeError):
    row_count: int

    def __init__(self, row_count: int) -> None:
        self.row_count = row_count
        super().__init__("Tesouro Transparente returned no usable rows")


def fetch_treasury_quotes() -> list[TreasuryQuote]:
    try:
        rows = _load_latest_csv_rows()
        if not rows:
            return []

        today = datetime.now()
        max_base: datetime | None = None
        latest: dict[tuple[str, str], dict] = {}

        for row in rows:
            bond_type = row.get("Tipo Titulo", "").strip()
            maturity_str = row.get("Data Vencimento", "").strip()
            base_str = row.get("Data Base", "").strip()

            if not maturity_str or not base_str:
                continue

            try:
                base_date = _parse_date(base_str)
            except (IndexError, ValueError):
                continue

            if max_base is None or base_date > max_base:
                max_base = base_date

            key = (bond_type, maturity_str)
            prev = latest.get(key)
            if prev is not None and base_date <= prev["_base_date"]:
                continue

            latest[key] = {
                "_base_date": base_date,
                "_base_str": base_str,
                "bond_type": bond_type,
                "maturity_str": maturity_str,
                "buy_rate": row.get("Taxa Compra Manha", ""),
                "sell_rate": row.get("Taxa Venda Manha", ""),
                "buy_price": row.get("PU Compra Manha", ""),
                "sell_price": row.get("PU Venda Manha", ""),
                "base_price": row.get("PU Base Manha", ""),
            }

        result: list[TreasuryQuote] = []
        for b in latest.values():
            if b["_base_date"] != max_base:
                continue

            bond_type = b["bond_type"]

            if not bond_type.lower().startswith(ALLOWED_BOND_TYPES):
                continue

            maturity_str = b["maturity_str"]

            try:
                maturity_date = _parse_date(maturity_str)
                duration = (maturity_date - today).days
                if duration < 0:
                    duration = 0
            except (IndexError, ValueError):
                duration = 0

            if duration <= 0:
                continue

            maturity_label = _get_maturity_label(duration) if duration else "N/A"

            buy_rate = _parse_rate(b["buy_rate"])
            sell_rate = _parse_rate(b["sell_rate"])
            buy_price = _parse_rate(b["buy_price"])
            sell_price = _parse_rate(b["sell_price"])

            indexer = "prefixado"
            if "ipca" in bond_type.lower():
                indexer = "ipca"
            elif "selic" in bond_type.lower():
                indexer = "selic"

            symbol_base = bond_type.lower().replace(" ", "_").replace("+", "plus")
            symbol_maturity = maturity_str.replace("/", "-")

            result.append({
                "symbol": f"{symbol_base}_{symbol_maturity}",
                "name": bond_type,
                "indexer": indexer,
                "maturityDate": maturity_str,
                "baseDate": b["_base_str"],
                "durationDays": duration,
                "maturityLabel": maturity_label,
                "buyRate": buy_rate,
                "sellRate": sell_rate,
                "buyPrice": buy_price,
                "sellPrice": sell_price,
                "basePrice": _parse_rate(b["base_price"]),
                "couponType": "semestrais" if "juros" in bond_type.lower() else "zero",
            })

        return result

    except (IndexError, KeyError, TypeError, ValueError):
        return []


def fetch_yield_curve() -> YieldCurve:
    bonds = fetch_treasury_quotes()

    if not bonds:
        return {"error": "No data available", "prefixado": [], "ipca": []}

    prefixado = [
        b for b in bonds
        if b["indexer"] == "prefixado"
        and b["maturityDate"]
        and b["buyRate"] is not None
    ]
    ipca = [
        b for b in bonds
        if b["indexer"] == "ipca"
        and b["maturityDate"]
        and b["buyRate"] is not None
    ]

    prefixado.sort(key=lambda x: x["durationDays"])
    ipca.sort(key=lambda x: x["durationDays"])

    return {
        "prefixado": prefixado,
        "ipca": ipca,
    }


_CSV_CACHE: list[dict] | None = None
_LATEST_CSV_CACHE: list[dict] | None = None


def _load_csv_rows() -> list[dict] | None:
    global _CSV_CACHE
    if _CSV_CACHE is not None:
        return _CSV_CACHE
    rows = query_treasury_rows()
    if not rows:
        return None
    _CSV_CACHE = rows
    return rows


def _load_latest_csv_rows() -> list[dict] | None:
    global _LATEST_CSV_CACHE
    if _LATEST_CSV_CACHE is not None:
        return _LATEST_CSV_CACHE
    rows = query_treasury_latest_rows()
    if not rows:
        return None
    _LATEST_CSV_CACHE = rows
    return rows


def _download_csv_rows() -> list[dict]:
    resp = httpx.get(
        TESOURO_TRANSPARENTE_CSV,
        headers={"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36"},
        timeout=REQUEST_TIMEOUT,
        follow_redirects=True,
    )
    resp.raise_for_status()
    csv_text = resp.content.decode("utf-8-sig")
    return list(csv.DictReader(StringIO(csv_text), delimiter=";"))


def refresh_treasury_data() -> dict[str, int]:
    global _CSV_CACHE, _LATEST_CSV_CACHE
    rows = _download_csv_rows()
    persisted: list[dict] = []
    for row in rows:
        bond_type = row.get("Tipo Titulo", "").strip()
        maturity = row.get("Data Vencimento", "").strip()
        if not bond_type or not maturity:
            continue
        persisted.append({**row, "_bond_code": _bond_code(bond_type, maturity)})
    row_count = replace_treasury_rows(persisted)
    if row_count == 0:
        raise TreasuryRefreshError(row_count)
    _CSV_CACHE = None
    _LATEST_CSV_CACHE = None
    set_meta("td_last_refresh", datetime.now().isoformat())
    return {"rows": row_count}


def fetch_bonds_history(codes: list[int], days: int = 90) -> list[dict]:
    rows = _load_csv_rows()
    return build_bonds_history(rows or [], codes, days)


def fetch_bond_catalog() -> list[dict]:
    rows = _load_csv_rows()
    return build_bond_catalog(rows or [])


def fetch_bond_history(code: int | str, days: int = 30) -> dict | None:
    rows = _load_csv_rows()
    return build_bond_history(rows or [], int(code), days)


def read_treasury_quotes() -> list[TreasuryQuote]:
    return fetch_treasury_quotes()


def read_yield_curve() -> YieldCurve:
    return fetch_yield_curve()


def read_bond_catalog() -> list[dict]:
    return fetch_bond_catalog()


def read_bond_history(code: int | str, days: int = 30) -> dict | None:
    return fetch_bond_history(code, days)


def read_bonds_history(codes: list[int], days: int = 90) -> list[dict]:
    return fetch_bonds_history(codes, days)
