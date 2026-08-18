import httpx
import csv
import json
import time
from io import StringIO
from datetime import datetime
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
        return []

    try:
        data = resp.json()
    except Exception:
        return []

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
    return result


def fetch_bond_history(code: int | str, days: int = 30) -> dict | None:
    cache_key = f"td_history_{code}_{days}"
    cached = _cache_get(cache_key, HISTORY_CACHE_TTL)
    if cached is not None:
        return cached

    params = {"days": days} if days and days > 0 else {}
    resp = _fetch_with_retry(TESOURO_HISTORIC_API.format(code=code), params)
    if resp is None:
        return None

    try:
        data = resp.json()
    except Exception:
        return None

    bond = data.get("TrsrBd")
    if not bond:
        return None

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
