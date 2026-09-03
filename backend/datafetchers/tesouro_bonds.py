import zlib
from datetime import datetime, timedelta


MAIN_MATURITIES = {
    "Curto (~2 anos)": (0, 730),
    "Médio (~5 anos)": (730, 1825),
    "Longo (~10 anos)": (1825, 3650),
    "Muito Longo (~15 anos)": (3650, 5475),
    "Ultra Longo (20+ anos)": (5475, 99999),
}

ALLOWED_BOND_TYPES = (
    "tesouro prefixado",
    "tesouro ipca+",
)


def _get_maturity_label(duration_days: int) -> str:
    for label, (min_days, max_days) in MAIN_MATURITIES.items():
        if min_days <= duration_days < max_days:
            return label
    return "Ultra Longo (20+ anos)"


def _parse_date(date_str: str) -> datetime:
    parts = date_str.split("/")
    return datetime(int(parts[2]), int(parts[1]), int(parts[0]))


def _parse_rate(rate_str: str) -> float | None:
    if not rate_str or not rate_str.strip():
        return None
    try:
        return float(rate_str.replace(",", "."))
    except ValueError:
        return None


def _bond_code(bond_type: str, maturity: str) -> int:
    return zlib.crc32(f"{bond_type}|{maturity}".encode("utf-8")) & 0x7FFFFFFF


def _latest_base(rows: list[dict]) -> datetime | None:
    latest: datetime | None = None
    for row in rows:
        base_str = row.get("Data Base", "").strip()
        if not base_str:
            continue
        try:
            base = _parse_date(base_str)
        except (IndexError, ValueError):
            continue
        if latest is None or base > latest:
            latest = base
    return latest


def _indexer(bond_type: str) -> str:
    normalized = bond_type.lower()
    if "ipca" in normalized:
        return "ipca"
    if "selic" in normalized:
        return "selic"
    if "igpm" in normalized:
        return "igpm"
    return "prefixado"


def build_bond_catalog(rows: list[dict]) -> list[dict]:
    latest = _latest_base(rows)
    if latest is None:
        return []

    today = datetime.now()
    seen: set[tuple[str, str]] = set()
    result: list[dict] = []
    for row in rows:
        bond_type = row.get("Tipo Titulo", "").strip()
        maturity_str = row.get("Data Vencimento", "").strip()
        base_str = row.get("Data Base", "").strip()
        if not bond_type or not maturity_str or not base_str:
            continue
        try:
            base_date = _parse_date(base_str)
            maturity_date = _parse_date(maturity_str)
        except (IndexError, ValueError):
            continue
        if base_date != latest or not bond_type.lower().startswith(ALLOWED_BOND_TYPES):
            continue
        if (maturity_date - today).days <= 0:
            continue
        key = (bond_type, maturity_str)
        if key in seen:
            continue
        seen.add(key)
        result.append({
            "code": _bond_code(bond_type, maturity_str),
            "name": bond_type,
            "indexer": _indexer(bond_type),
            "targetYear": maturity_date.year,
            "couponType": "semestrais" if "juros" in bond_type.lower() else "zero",
            "available": True,
        })

    result.sort(key=lambda bond: (0 if bond["indexer"] == "prefixado" else 1, bond["targetYear"] or 0))
    return result


def _match_codes(rows: list[dict], requested: set[int]) -> dict[int, tuple[str, str]]:
    matches: dict[int, tuple[str, str]] = {}
    seen: set[tuple[str, str]] = set()
    for row in rows:
        bond_type = row.get("Tipo Titulo", "").strip()
        maturity = row.get("Data Vencimento", "").strip()
        if not bond_type or not maturity:
            continue
        key = (bond_type, maturity)
        if key in seen:
            continue
        seen.add(key)
        code = _bond_code(bond_type, maturity)
        if code in requested:
            matches[code] = key
    return matches


def build_bonds_history(rows: list[dict], codes: list[int], days: int) -> list[dict]:
    normalized_codes = [int(code) for code in codes]
    latest = _latest_base(rows)
    if latest is None or not normalized_codes:
        return []

    matches = _match_codes(rows, set(normalized_codes))
    code_by_key = {key: code for code, key in matches.items()}
    cutoff = latest - timedelta(days=days) if days > 0 else None
    points_by_code: dict[int, list[dict]] = {code: [] for code in matches}

    for row in rows:
        key = (
            row.get("Tipo Titulo", "").strip(),
            row.get("Data Vencimento", "").strip(),
        )
        code = code_by_key.get(key)
        if code is None:
            continue
        base_str = row.get("Data Base", "").strip()
        try:
            base_date = _parse_date(base_str)
        except (IndexError, ValueError):
            continue
        if cutoff is not None and base_date < cutoff:
            continue
        points_by_code[code].append({
            "date": base_str,
            "sellRate": _parse_rate(row.get("Taxa Venda Manha", "")),
            "buyRate": _parse_rate(row.get("Taxa Compra Manha", "")),
            "sellPrice": _parse_rate(row.get("PU Venda Manha", "")),
            "buyPrice": _parse_rate(row.get("PU Compra Manha", "")),
        })

    result: list[dict] = []
    for code in normalized_codes:
        points = points_by_code.get(code)
        if not points:
            continue
        points.sort(key=lambda point: _parse_date(point["date"]))
        bond_type, maturity = matches[code]
        result.append({
            "code": code,
            "name": bond_type,
            "maturityDate": maturity,
            "points": points,
        })
    return result


def build_bond_history(rows: list[dict], code: int, days: int) -> dict | None:
    histories = build_bonds_history(rows, [int(code)], days)
    return histories[0] if histories else None
