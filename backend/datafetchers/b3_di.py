import io
import time
import zipfile
import xml.etree.ElementTree as ET
from concurrent.futures import ThreadPoolExecutor
from datetime import date, datetime, timedelta

import httpx

from db.store import upsert_b3_di, query_b3_di, get_b3_di_dates

B3_SPR_URL = "https://www.b3.com.br/pesquisapregao/download?filelist=SPRD{YYMMDD}.zip"
REQUEST_TIMEOUT = 30
XML_NS = "urn:bvmf.217.01.xsd"

BROWSER_HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36",
}

DI_PREFIX = "DI1"
DI_MONTHS = {"F": 1, "G": 2, "H": 3, "J": 4, "K": 5, "M": 6, "N": 7, "Q": 8, "U": 9, "V": 10, "X": 11, "Z": 12}

MAX_DOWNLOAD_WORKERS = 3
DOWNLOAD_RETRIES = 2
DOWNLOAD_RETRY_SLEEP = 1.5


def _easter(year: int) -> date:
    a = year % 19
    b = year // 100
    c = year % 100
    d = b // 4
    e = b % 4
    f = (b + 8) // 25
    g = (b - f + 1) // 3
    h = (19 * a + b - d - g + 15) % 30
    i = c // 4
    k = c % 4
    l = (32 + 2 * e + 2 * i - h - k) % 7
    m = (a + 11 * h + 22 * l) // 451
    month = (h + l - 7 * m + 114) // 31
    day = ((h + l - 7 * m + 114) % 31) + 1
    return date(year, month, day)


def _holidays(year: int) -> set[date]:
    easter = _easter(year)
    fixed = [
        date(year, 1, 1),
        date(year, 4, 21),
        date(year, 5, 1),
        date(year, 9, 7),
        date(year, 10, 12),
        date(year, 11, 2),
        date(year, 11, 15),
        date(year, 11, 20),
        date(year, 12, 25),
    ]
    movable = [
        easter - timedelta(days=47),
        easter - timedelta(days=2),
        easter + timedelta(days=60),
    ]
    return set(fixed) | set(movable)


def is_business_day(d: date) -> bool:
    if d.weekday() >= 5:
        return False
    return d not in _holidays(d.year)


def _first_business_day(year: int, month: int) -> date:
    d = date(year, month, 1)
    while not is_business_day(d):
        d += timedelta(days=1)
    return d


def decode_maturity(symbol: str) -> str | None:
    if not symbol.startswith(DI_PREFIX) or len(symbol) != 6:
        return None
    month_letter = symbol[3]
    yy = symbol[4:6]
    if month_letter not in DI_MONTHS:
        return None
    try:
        year = 2000 + int(yy)
    except ValueError:
        return None
    maturity = _first_business_day(year, DI_MONTHS[month_letter])
    return maturity.isoformat()


def _parse_spr(data: bytes) -> list[dict]:
    try:
        outer = zipfile.ZipFile(io.BytesIO(data))
    except zipfile.BadZipFile:
        return []
    records: list[dict] = []

    def handle_entry(name: str):
        raw = outer.read(name)
        try:
            inner = zipfile.ZipFile(io.BytesIO(raw))
            xml_names = inner.namelist()
        except zipfile.BadZipFile:
            xml_names = []
            if name.endswith(".xml"):
                xml_names = [name]
                inner = None
        for xml_name in xml_names:
            xml_bytes = inner.read(xml_name) if inner is not None else raw
            records.extend(_parse_xml(xml_bytes))

    for name in outer.namelist():
        handle_entry(name)
    seen: set[tuple[str, str]] = set()
    unique: list[dict] = []
    for r in records:
        key = (r["trade_date"], r["symbol"])
        if key not in seen:
            seen.add(key)
            unique.append(r)
    return unique


def _parse_xml(data: bytes) -> list[dict]:
    try:
        root = ET.fromstring(data)
    except ET.ParseError:
        return []
    records: list[dict] = []
    for pr in root.iter(f"{{{XML_NS}}}PricRpt"):
        trad_dt = pr.find(f"{{{XML_NS}}}TradDt/{{{XML_NS}}}Dt")
        scty = pr.find(f"{{{XML_NS}}}SctyId/{{{XML_NS}}}TckrSymb")
        attrs = pr.find(f"{{{XML_NS}}}FinInstrmAttrbts")
        if trad_dt is None or scty is None or scty.text is None:
            continue
        symbol = scty.text.strip()
        if not symbol.startswith(DI_PREFIX):
            continue
        maturity = decode_maturity(symbol)
        if maturity is None:
            continue
        rate = None
        if attrs is not None:
            tax = attrs.find(f"{{{XML_NS}}}AdjstdQtTax")
            if tax is not None and tax.text:
                try:
                    rate = float(tax.text)
                except ValueError:
                    rate = None
        records.append({
            "trade_date": trad_dt.text.strip(),
            "symbol": symbol,
            "maturity": maturity,
            "rate": rate,
        })
    return records


def _download_day(d: date) -> list[dict]:
    url = B3_SPR_URL.format(YYMMDD=d.strftime("%y%m%d"))
    last_err: Exception | None = None
    for attempt in range(DOWNLOAD_RETRIES):
        try:
            resp = httpx.get(url, headers=BROWSER_HEADERS, timeout=REQUEST_TIMEOUT, follow_redirects=True)
            resp.raise_for_status()
            records = _parse_spr(resp.content)
            if not records:
                return []
            for r in records:
                r["trade_date"] = d.isoformat()
            upsert_b3_di(records)
            return records
        except Exception as e:
            last_err = e
            print(f"[B3-DI] Erro download {d} tentativa {attempt + 1}: {e}")
            if attempt < DOWNLOAD_RETRIES - 1:
                time.sleep(DOWNLOAD_RETRY_SLEEP)
    print(f"[B3-DI] Falha definitiva {d}: {last_err}")
    return []


def list_business_days(end: date, n: int) -> list[date]:
    result = []
    d = end
    while len(result) < n and d.year >= 2000:
        if is_business_day(d):
            result.append(d)
        d -= timedelta(days=1)
    return list(reversed(result))


def fetch_di_curves(days: int = 30, use_cache: bool = True) -> dict[str, list[dict]]:
    available = set(get_b3_di_dates())
    targets = list_business_days(date.today(), days)
    to_download = [d for d in targets if d.isoformat() not in available]

    if to_download and (not use_cache or len(to_download) <= days):
        with ThreadPoolExecutor(max_workers=MAX_DOWNLOAD_WORKERS) as ex:
            list(ex.map(_download_day, to_download))

    stored = query_b3_di()
    curves: dict[str, list[dict]] = {}
    for r in stored:
        curves.setdefault(r["trade_date"], []).append({
            "symbol": r["symbol"],
            "maturity": r["maturity"],
            "rate": r["rate"],
        })
    ordered = {}
    for d in targets:
        iso = d.isoformat()
        if iso in curves:
            ordered[iso] = sorted(curves[iso], key=lambda p: p["maturity"])
    return ordered


def force_refresh_di(days: int = 30) -> dict[str, list[dict]]:
    return fetch_di_curves(days=days, use_cache=False)