import io
import time
import xml.etree.ElementTree as ET
import zipfile
from concurrent.futures import ThreadPoolExecutor
from contextvars import copy_context
from datetime import date, datetime, timedelta, timezone
from typing import cast

import httpx

from db.cached_reads import query_recent_di_curves
from db.store import (
    get_b3_di_dates,
    query_b3_di,
    query_observation_vintages,
    upsert_b3_di,
)
from derived.real_rate_inputs import load_focus_expectations
from ingestion_policy import ErrorCategory, current_retry_budget
from provider_http import BINARY_MAX_BYTES, get_provider_response
from source_diagnostics import source_request_context
from source_ingestion import B3DiIngestion, FetchResult, acquisition_unit

B3_SPR_URL = "https://www.b3.com.br/pesquisapregao/download?filelist=SPRD{YYMMDD}.zip"
REQUEST_TIMEOUT = 30
XML_NS = "urn:bvmf.217.01.xsd"

BROWSER_HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36",
}

DI_PREFIX = "DI1"
DI_MONTHS = {"F": 1, "G": 2, "H": 3, "J": 4, "K": 5, "M": 6, "N": 7, "Q": 8, "U": 9, "V": 10, "X": 11, "Z": 12}
DI_MATURITY_RULE_VERSION = "local-calendar-unverified-v1"
VERIFIED_DI_MATURITY_BASIS = "b3_official_calendar_v1"


class UnsupportedDIMaturityCalendar(ValueError):
    """Raised when a contract maturity cannot be supported by a verified calendar."""

MAX_DOWNLOAD_WORKERS = 2
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


def maturity_provenance(symbol: str, maturity: str) -> str:
    """Label existing locally inferred maturities; never imply official validation."""
    expected = decode_maturity(symbol)
    if expected is None or expected != maturity:
        raise UnsupportedDIMaturityCalendar(
            f"unsupported or inconsistent DI1 maturity for {symbol}: {maturity}"
        )
    return DI_MATURITY_RULE_VERSION


def require_verified_maturity_basis(basis: str) -> None:
    """Fail closed until an official, versioned session-calendar basis exists."""
    if basis != VERIFIED_DI_MATURITY_BASIS:
        raise UnsupportedDIMaturityCalendar(
            f"unsupported DI maturity provenance: {basis}"
        )


def _parse_spr(data: bytes) -> list[dict]:
    if not data:
        raise EmptyB3ResponseError("empty_http_response")
    try:
        outer = zipfile.ZipFile(io.BytesIO(data))
    except zipfile.BadZipFile as exc:
        raise InvalidB3ArchiveError("invalid_zip_archive") from exc
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


class EmptyB3ResponseError(ValueError):
    """The provider returned no archive bytes; this is not an invalid ZIP."""

    error_category = ErrorCategory.ENVELOPE_ERROR


class InvalidB3ArchiveError(ValueError):
    """Non-empty provider response does not contain a valid ZIP archive."""

    error_category = ErrorCategory.INVALID_SCHEMA


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
        security_id = None
        fin_id = pr.find(f"{{{XML_NS}}}FinInstrmId/{{{XML_NS}}}OthrId/{{{XML_NS}}}Id")
        if fin_id is None:
            fin_id = pr.find(f"{{{XML_NS}}}SctyId/{{{XML_NS}}}ISIN")
        if fin_id is not None and fin_id.text:
            security_id = fin_id.text.strip()
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
            "maturity_basis": maturity_provenance(symbol, maturity),
            "security_id": security_id,
            "rate": rate,
        })
    return records


def parse_di_xml(data: bytes) -> list[dict[str, object]]:
    """Public pure parser entry point for isolated retrospective ingestion."""
    return cast(list[dict[str, object]], _parse_xml(data))


def _download_day_untracked(d: date) -> list[dict]:
    url = B3_SPR_URL.format(YYMMDD=d.strftime("%y%m%d"))
    filename = f"SPRD{d:%y%m%d}.zip"
    last_err: Exception | None = None
    response = None
    for attempt in range(DOWNLOAD_RETRIES):
        try:
            with source_request_context(
                provider="b3", trade_date=d.isoformat(), file_name=filename,
                local_attempt=attempt + 1,
            ):
                resp = get_provider_response(
                    url,
                    request=httpx.get,
                    headers=BROWSER_HEADERS,
                    timeout=REQUEST_TIMEOUT,
                    max_bytes=BINARY_MAX_BYTES,
                    route_id="b3_spr",
                )
            response = resp
            break
        except (httpx.TransportError, httpx.HTTPStatusError) as e:
            last_err = e
            print(f"[B3-DI] Erro download {d} tentativa {attempt + 1}: {type(e).__name__}")
            if attempt < DOWNLOAD_RETRIES - 1:
                time.sleep(DOWNLOAD_RETRY_SLEEP)
    if response is None:
        raise RuntimeError(f"B3 DI provider request failed for {d}") from last_err
    if not response.content:
        raise EmptyB3ResponseError("empty_http_response")
    records = _parse_spr(response.content)
    if not records:
        return []
    for record in records:
        record["trade_date"] = d.isoformat()
    upsert_b3_di(records)
    return records


_download_day = _download_day_untracked


def list_business_days(end: date, n: int) -> list[date]:
    result = []
    d = end
    while len(result) < n and d.year >= 2000:
        if is_business_day(d):
            result.append(d)
        d -= timedelta(days=1)
    return list(reversed(result))


def _fetch_di_curves_untracked(days: int = 30, use_cache: bool = True) -> FetchResult:
    available = set(get_b3_di_dates())
    targets = list_business_days(datetime.now(timezone.utc).date(), days)
    to_download = [d for d in targets if d.isoformat() not in available]
    if use_cache:
        cached_targets = [d for d in targets if d.isoformat() in available]
        if cached_targets:
            vintages = query_observation_vintages("b3", "di1")
            vintaged_dates = {row["reference_period"] for row in vintages}
            repair_dates = {max(cached_targets)}
            focus_expectations = load_focus_expectations(
                as_of=datetime.now(timezone.utc)
            )
            if focus_expectations:
                latest_focus_reference = max(
                    expectation.reference_date for expectation in focus_expectations
                )
                focus_aligned = max(
                    (
                        day
                        for day in cached_targets
                        if day <= latest_focus_reference
                    ),
                    default=None,
                )
                if focus_aligned is not None:
                    repair_dates.add(focus_aligned)
            to_download.extend(
                day
                for day in sorted(repair_dates)
                if day.isoformat() not in vintaged_dates
            )

    downloaded: list[list[dict]] = []
    should_download = bool(to_download and (not use_cache or len(to_download) <= days))
    if should_download:
        budget = current_retry_budget()
        if budget is not None:
            budget.set_request_limit(len(to_download) * 2)

        def download(day: date) -> list[dict]:
            if budget is None:
                return _download_day(day)
            with acquisition_unit(f"b3:{day.isoformat()}", 2, 60.0):
                return _download_day(day)

        contexts = [copy_context() for _ in to_download]
        with ThreadPoolExecutor(max_workers=MAX_DOWNLOAD_WORKERS) as ex:
            downloaded = list(
                ex.map(lambda pair: pair[0].run(download, pair[1]), zip(contexts, to_download))
            )

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
    downloaded_records = [record for batch in downloaded for record in batch]
    reference_dates = (
        [record["trade_date"] for record in downloaded_records]
        if should_download
        else list(ordered)
    )
    return FetchResult(
        ordered,
        network_called=should_download,
        received_count=len(downloaded_records),
        valid_count=len(downloaded_records),
        reference_min=min(reference_dates, default=None),
        reference_max=max(reference_dates, default=None),
        request_count=len(to_download) if should_download else 0,
        status="empty" if not should_download and not ordered else None,
    )


def fetch_di_curves(days: int = 30, use_cache: bool = True) -> dict[str, object]:
    result = B3DiIngestion().fetch(
        indicator="di_curve",
        qualifiers={"days": days, "use_cache": use_cache},
        operation=lambda: _fetch_di_curves_untracked(days, use_cache),
        network_expected=True,
    )
    return cast(dict[str, object], result)


def read_di_curves(days: int = 30) -> dict[str, list[dict]]:
    return query_recent_di_curves(days)


def force_refresh_di(days: int = 30) -> dict[str, list[dict]]:
    return fetch_di_curves(days=days, use_cache=False)
