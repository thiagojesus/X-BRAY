import httpx
import csv
import json
import time
import zlib
from io import StringIO
from datetime import datetime, timedelta
from db.store import set_meta, get_meta

TESOURO_TRANSPARENTE_CSV = "https://www.tesourotransparente.gov.br/ckan/dataset/taxas-dos-titulos-ofertados-pelo-tesouro-direto/resource/796d2059-14e9-44e3-80c9-2d9e30b405c1/download"
TESOURO_LIST_API = "https://www.tesourodireto.com.br/o/c/rentabilidades/"
TESOURO_HISTORIC_API = "https://www.tesourodireto.com.br/o/historico-rentabilidade/{code}"
REQUEST_TIMEOUT = 30

CATALOG_CACHE_TTL = 6 * 3600
HISTORY_CACHE_TTL = 6 * 3600

BROWSER_HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36",
    "Accept": "application/json",
    "Referer": "https://www.tesourodireto.com.br/produtos/dados-sobre-titulos/historico-de-precos-e-taxas",
}

INDEXER_MAP = {
    17: "selic",
    19: "prefixado",
    22: "ipca",
    1: "igpm",
}

COUPON_MAP = {
    "U": "zero",
    "S": "semestrais",
    "M": "mensais",
}

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


def _needs_refresh() -> bool:
    last = get_meta("td_last_refresh")
    if not last:
        return True
    try:
        last_dt = datetime.fromisoformat(last)
        return (datetime.now() - last_dt).total_seconds() > 23 * 3600
    except Exception:
        return True


def _get_maturity_label(duration_days: int) -> str:
    for label, (min_days, max_days) in MAIN_MATURITIES.items():
        if min_days <= duration_days < max_days:
            return label
    return "Ultra Longo (20+ anos)"


def _parse_date(date_str: str) -> datetime:
    parts = date_str.split("/")
    return datetime(int(parts[2]), int(parts[1]), int(parts[0]))


def _parse_rate(rate_str: str) -> float | None:
    if not rate_str or rate_str.strip() == "":
        return None
    try:
        return float(rate_str.replace(",", "."))
    except ValueError:
        return None


def fetch_treasury_quotes() -> list[dict]:
    try:
        headers = {
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36",
        }
        resp = httpx.get(TESOURO_TRANSPARENTE_CSV, headers=headers, timeout=REQUEST_TIMEOUT, follow_redirects=True)
        resp.raise_for_status()

        csv_text = resp.content.decode("utf-8-sig")
        reader = csv.DictReader(StringIO(csv_text), delimiter=";")

        today = datetime.now()
        max_base: datetime | None = None
        latest: dict[tuple[str, str], dict] = {}

        for row in reader:
            bond_type = row.get("Tipo Titulo", "").strip()
            maturity_str = row.get("Data Vencimento", "").strip()
            base_str = row.get("Data Base", "").strip()

            if not maturity_str or not base_str:
                continue

            try:
                base_date = _parse_date(base_str)
            except Exception:
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

        result = []
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
            except Exception:
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

    except Exception as e:
        print(f"[TD] Error fetching treasury quotes: {e}")
        return []


def fetch_yield_curve() -> dict:
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


def _fetch_with_retry(url: str, params: dict, tries: int = 6) -> httpx.Response | None:
    for attempt in range(tries):
        try:
            resp = httpx.get(url, params=params, headers=BROWSER_HEADERS, timeout=REQUEST_TIMEOUT, follow_redirects=True)
            if resp.status_code == 200:
                return resp
        except Exception as e:
            print(f"[TD] {url} attempt {attempt} failed: {e}")
        time.sleep(0.6 * (attempt + 1))
    return None


def _cache_get(key: str, ttl: int) -> object | None:
    raw = get_meta(key)
    ts = get_meta(f"{key}_ts")
    if not raw or not ts:
        return None
    try:
        cached_at = datetime.fromisoformat(ts)
        if (datetime.now() - cached_at).total_seconds() > ttl:
            return None
        return json.loads(raw)
    except Exception:
        return None


def _cache_set(key: str, payload: object):
    set_meta(key, json.dumps(payload, ensure_ascii=False, default=str))
    set_meta(f"{key}_ts", datetime.now().isoformat())


# ---------------------------------------------------------------------------
# CSV fallback source (Tesouro Transparente)
# The tesourodireto.com.br JSON APIs (list + history) are protected by
# Cloudflare (403 "Just a moment..."). The official Transparente CSV is
# Cloudflare-free and carries the FULL history since 2007, so we derive both
# the bond catalog and per-bond history from it when the JSON API is blocked.
# ---------------------------------------------------------------------------

_CSV_CACHE: dict[str, object] = {}
_CSV_CACHE_KEY = "td_csv_rows"


def _bond_code(bond_type: str, maturity: str) -> int:
    """Stable integer code for a bond, derived from its type + maturity.

    The JSON API has real numeric treasuryBondCode values, but the CSV does
    not. We synthesize a deterministic int so the frontend select/history
    round-trip (int) works identically for both sources.
    """
    return zlib.crc32(f"{bond_type}|{maturity}".encode("utf-8")) & 0x7FFFFFFF


def _load_csv_rows() -> list[dict] | None:
    """Fetch and parse the Transparente CSV once per TTL (module-level memo)."""
    global _CSV_CACHE
    cached = _CSV_CACHE.get(_CSV_CACHE_KEY)
    if cached is not None:
        cached_at = _CSV_CACHE.get(f"{_CSV_CACHE_KEY}_ts")
        if cached_at is not None:
            age = (datetime.now() - cached_at).total_seconds()
            if age <= CATALOG_CACHE_TTL:
                return cached  # type: ignore[return-value]
    try:
        resp = httpx.get(
            TESOURO_TRANSPARENTE_CSV,
            headers={"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36"},
            timeout=REQUEST_TIMEOUT,
            follow_redirects=True,
        )
        resp.raise_for_status()
        csv_text = resp.content.decode("utf-8-sig")
        reader = csv.DictReader(StringIO(csv_text), delimiter=";")
        rows = list(reader)
        if not rows:
            return None
        _CSV_CACHE = {_CSV_CACHE_KEY: rows, f"{_CSV_CACHE_KEY}_ts": datetime.now()}
        return rows
    except Exception as e:
        print(f"[TD] CSV load failed: {e}")
        return None


def _csv_latest_base(rows: list[dict]) -> datetime | None:
    """Latest 'Data Base' across all CSV rows."""
    latest: datetime | None = None
    for r in rows:
        base_str = r.get("Data Base", "").strip()
        if not base_str:
            continue
        try:
            base = _parse_date(base_str)
        except Exception:
            continue
        if latest is None or base > latest:
            latest = base
    return latest


def _csv_catalog() -> list[dict]:
    """Derive the bond catalog from the CSV (only currently offered bonds)."""
    rows = _load_csv_rows()
    if not rows:
        return []
    latest = _csv_latest_base(rows)
    if latest is None:
        return []

    today = datetime.now()
    seen: set[tuple[str, str]] = set()
    result: list[dict] = []
    for r in rows:
        bond_type = r.get("Tipo Titulo", "").strip()
        maturity_str = r.get("Data Vencimento", "").strip()
        base_str = r.get("Data Base", "").strip()
        if not bond_type or not maturity_str or not base_str:
            continue
        try:
            base_date = _parse_date(base_str)
            maturity_date = _parse_date(maturity_str)
        except Exception:
            continue
        if base_date != latest:
            continue
        if not bond_type.lower().startswith(ALLOWED_BOND_TYPES):
            continue
        duration = (maturity_date - today).days
        if duration <= 0:
            continue
        key = (bond_type, maturity_str)
        if key in seen:
            continue
        seen.add(key)

        indexer = "prefixado"
        if "ipca" in bond_type.lower():
            indexer = "ipca"
        elif "selic" in bond_type.lower():
            indexer = "selic"
        elif "igpm" in bond_type.lower():
            indexer = "igpm"

        result.append({
            "code": _bond_code(bond_type, maturity_str),
            "name": bond_type,
            "indexer": indexer,
            "targetYear": maturity_date.year,
            "couponType": "semestrais" if "juros" in bond_type.lower() else "zero",
            "available": True,
        })

    result.sort(key=lambda b: (0 if b["indexer"] == "prefixado" else 1, b["targetYear"] or 0))
    return result


def _csv_history(code: int, days: int) -> dict | None:
    """Derive per-bond history from the CSV for a synthetic code."""
    rows = _load_csv_rows()
    if not rows:
        return None
    latest = _csv_latest_base(rows)
    if latest is None:
        return None

    # Find the matching (bond_type, maturity) for this code.
    seen: set[tuple[str, str]] = set()
    match: tuple[str, str] | None = None
    for r in rows:
        bond_type = r.get("Tipo Titulo", "").strip()
        maturity_str = r.get("Data Vencimento", "").strip()
        if not bond_type or not maturity_str:
            continue
        key = (bond_type, maturity_str)
        if key in seen:
            continue
        seen.add(key)
        if _bond_code(bond_type, maturity_str) == code:
            match = key
            break
    if match is None:
        return None
    bond_type, maturity_str = match

    cutoff = latest - timedelta(days=days) if days and days > 0 else None
    points: list[dict] = []
    for r in rows:
        if r.get("Tipo Titulo", "").strip() != bond_type:
            continue
        if r.get("Data Vencimento", "").strip() != maturity_str:
            continue
        base_str = r.get("Data Base", "").strip()
        try:
            base_date = _parse_date(base_str)
        except Exception:
            continue
        if cutoff is not None and base_date < cutoff:
            continue
        points.append({
            "date": base_str,
            "sellRate": _parse_rate(r.get("Taxa Venda Manha", "")),
            "buyRate": _parse_rate(r.get("Taxa Compra Manha", "")),
            "sellPrice": _parse_rate(r.get("PU Venda Manha", "")),
            "buyPrice": _parse_rate(r.get("PU Compra Manha", "")),
        })

    if not points:
        return None
    points.sort(key=lambda p: _parse_date(p["date"]))
    return {
        "code": code,
        "name": bond_type,
        "maturityDate": maturity_str,
        "points": points,
    }


def fetch_bond_catalog() -> list[dict]:
    cached = _cache_get("td_catalog", CATALOG_CACHE_TTL)
    if cached is not None:
        return cached

    resp = _fetch_with_retry(TESOURO_LIST_API, {
        "fields": "treasuryBondCode,treasuryBondSimplifiedName,financialIndexerCode,targetYear,typeReceiptInterest,unitaryInvestmentValue,availableInvestmentBondQuantity,segmentCode",
        "sort": "segmentCode:asc,treasuryBondSimplifiedName:desc,typeReceiptInterest:asc",
        "pageSize": 500,
    })
    if resp is None:
        return _csv_catalog()

    try:
        data = resp.json()
    except Exception:
        return _csv_catalog()

    result = []
    for item in data.get("items", []):
        code = item.get("treasuryBondCode")
        name = item.get("treasuryBondSimplifiedName")
        if not code or not name:
            continue

        indexer_code = item.get("financialIndexerCode")
        indexer = INDEXER_MAP.get(indexer_code, "outro")

        result.append({
            "code": code,
            "name": name,
            "indexer": indexer,
            "targetYear": item.get("targetYear"),
            "couponType": COUPON_MAP.get(item.get("typeReceiptInterest"), "zero"),
            "unitValue": item.get("unitaryInvestmentValue"),
            "available": (item.get("availableInvestmentBondQuantity") or 0) > 0,
        })

    if result:
        _cache_set("td_catalog", result)
    else:
        result = _csv_catalog()
        if result:
            _cache_set("td_catalog", result)
    return result


def fetch_bond_history(code: int | str, days: int = 30) -> dict | None:
    cache_key = f"td_history_{code}_{days}"
    cached = _cache_get(cache_key, HISTORY_CACHE_TTL)
    if cached is not None:
        return cached

    params = {"days": days} if days and days > 0 else {}
    resp = _fetch_with_retry(TESOURO_HISTORIC_API.format(code=code), params)
    if resp is None:
        return _csv_history(int(code), days)

    try:
        data = resp.json()
    except Exception:
        return _csv_history(int(code), days)

    bond = data.get("TrsrBd")
    if not bond:
        return _csv_history(int(code), days)

    labels = bond.get("label") or []
    red_rate = bond.get("anulRedRate") or []
    inv_rate = bond.get("anulInvstmtRate") or []
    red_val = bond.get("untrRedVal") or []
    inv_val = bond.get("untrInvstmtVal") or []

    points = []
    for i, label in enumerate(labels):
        points.append({
            "date": label,
            "sellRate": red_rate[i] if i < len(red_rate) else None,
            "buyRate": inv_rate[i] if i < len(inv_rate) else None,
            "sellPrice": red_val[i] if i < len(red_val) else None,
            "buyPrice": inv_val[i] if i < len(inv_val) else None,
        })

    result = {
        "code": code,
        "name": bond.get("nm"),
        "maturityDate": bond.get("mtrtyDt"),
        "points": points,
    }

    if points:
        _cache_set(cache_key, result)
    return result
