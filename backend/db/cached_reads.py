import json
from collections.abc import Mapping, Sequence

from db.store import _parse_sgs_date, _query, _to_sgs_date


def _iso_date(value: str | None) -> str | None:
    if value is None or "/" not in value:
        return value
    return _parse_sgs_date(value)


def query_sgs_batch(
    codes: Mapping[str, int],
    start_date: str | None = None,
    end_date: str | None = None,
) -> dict[str, list[dict]]:
    if not codes:
        return {}

    placeholders = ", ".join("?" for _ in codes)
    sql = f"SELECT series_code, date, value FROM sgs WHERE series_code IN ({placeholders})"
    params: list[int | str] = list(codes.values())
    normalized_start = _iso_date(start_date)
    normalized_end = _iso_date(end_date)
    if normalized_start is not None:
        sql += " AND date >= ?"
        params.append(normalized_start)
    if normalized_end is not None:
        sql += " AND date <= ?"
        params.append(normalized_end)
    sql += " ORDER BY series_code ASC, date ASC"

    rows = _query(sql, params)
    names_by_code = {code: name for name, code in codes.items()}
    result: dict[str, list[dict]] = {name: [] for name in codes}
    for row in rows:
        name = names_by_code[row["series_code"]]
        result[name].append({"data": _to_sgs_date(row["date"]), "valor": str(row["value"])})
    return result


def query_focus_batch(indicators: Sequence[str]) -> dict[str, list[dict]]:
    if not indicators:
        return {}
    placeholders = ", ".join("?" for _ in indicators)
    rows = _query(
        f"SELECT indicator, data_json FROM focus WHERE indicator IN ({placeholders}) ORDER BY indicator, date",
        list(indicators),
    )
    result: dict[str, list[dict]] = {indicator: [] for indicator in indicators}
    for row in rows:
        result[row["indicator"]].append(json.loads(row["data_json"]))
    return result


def query_anbima_all() -> dict[str, list[dict]]:
    rows = _query("SELECT sheet_name, data_json FROM anbima ORDER BY sheet_name, row_index")
    result: dict[str, list[dict]] = {}
    for row in rows:
        result.setdefault(row["sheet_name"], []).append(json.loads(row["data_json"]))
    return result


def query_recent_di_curves(days: int) -> dict[str, list[dict]]:
    rows = _query(
        "SELECT trade_date, symbol, maturity, rate FROM b3_di "
        "WHERE trade_date IN (SELECT DISTINCT trade_date FROM b3_di ORDER BY trade_date DESC LIMIT ?) "
        "ORDER BY trade_date ASC, maturity ASC",
        (days,),
    )
    result: dict[str, list[dict]] = {}
    for row in rows:
        result.setdefault(row["trade_date"], []).append({
            "symbol": row["symbol"],
            "maturity": row["maturity"],
            "rate": row["rate"],
        })
    return result
