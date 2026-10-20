import json
from dataclasses import dataclass
from datetime import date, datetime
from math import isfinite
from typing import cast

from datafetchers.focus import FOCUS_12M_KEY
from db import store
from derived.models import utc_datetime


@dataclass(frozen=True, slots=True)
class FocusExpectation:
    reference_date: date
    median: float
    available_at: datetime
    availability_basis: str


@dataclass(frozen=True, slots=True)
class DIContract:
    trade_date: date
    symbol: str
    maturity: date
    rate: float
    available_at: datetime
    availability_basis: str
    security_id: str | None = None
    instrument_source_hash: str | None = None
    instrument_report_date: date | None = None
    instrument_available_at: datetime | None = None
    expiry_field: str | None = None
    instrument_isin: str | None = None
    fin_instrm_id: str | None = None
    instrument_source_name: str | None = None


def _json_object(value: object) -> dict[str, object] | None:
    try:
        parsed = json.loads(str(value))
    except (TypeError, ValueError):
        return None
    return cast(dict[str, object], parsed) if isinstance(parsed, dict) else None


def _finite_number(value: object) -> float | None:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    parsed = float(value)
    return parsed if isfinite(parsed) else None


def _aware_timestamp(value: object) -> datetime | None:
    try:
        return utc_datetime(datetime.fromisoformat(str(value).replace("Z", "+00:00")))
    except (TypeError, ValueError):
        return None


def parse_reference_date(value: object) -> date | None:
    """Parse a date-like reference value used by source and history rows."""
    try:
        return date.fromisoformat(str(value)[:10])
    except (TypeError, ValueError):
        return None


def _availability_basis(row: dict[str, object]) -> str:
    return "source_published_at" if row["source_published_at"] else "first_seen_at"


def load_focus_expectations(*, as_of: datetime) -> tuple[FocusExpectation, ...]:
    cutoff = utc_datetime(as_of)
    rows = store.query_rows(
        "SELECT reference_period, raw_value_json, source_published_at, first_seen_at, "
        "available_at, payload_hash FROM observation_vintages "
        "WHERE provider = ? AND series_key = ? AND available_at <= ? "
        "ORDER BY reference_period, available_at, first_seen_at, payload_hash",
        ("focus", FOCUS_12M_KEY, cutoff.isoformat()),
    )
    latest: dict[date, FocusExpectation] = {}
    for row in rows:
        reference_date = parse_reference_date(row["reference_period"])
        raw = _json_object(row["raw_value_json"])
        available_at = _aware_timestamp(row["available_at"])
        if reference_date is None or raw is None or available_at is None:
            continue
        median = _finite_number(raw.get("Mediana"))
        raw_date = parse_reference_date(raw.get("Data"))
        base_calculation = raw.get("baseCalculo")
        if (
            raw.get("Indicador") != "IPCA"
            or raw.get("Suavizada") != "S"
            or isinstance(base_calculation, bool)
            or base_calculation != 0
            or raw_date != reference_date
            or median is None
        ):
            continue
        latest[reference_date] = FocusExpectation(
            reference_date=reference_date,
            median=median,
            available_at=available_at,
            availability_basis=_availability_basis(row),
        )
    return tuple(latest[key] for key in sorted(latest))


def load_di_contracts(*, as_of: datetime) -> tuple[DIContract, ...]:
    cutoff = utc_datetime(as_of)
    rows = store.query_rows(
        "SELECT reference_period, dimensions_json, raw_value_json, source_published_at, "
        "first_seen_at, available_at, payload_hash FROM observation_vintages "
        "WHERE provider = ? AND series_key = ? AND available_at <= ? "
        "ORDER BY reference_period, dimensions_hash, available_at, first_seen_at, payload_hash",
        ("b3", "di1", cutoff.isoformat()),
    )
    latest: dict[tuple[date, str, date], DIContract] = {}
    for row in rows:
        trade_date = parse_reference_date(row["reference_period"])
        dimensions = _json_object(row["dimensions_json"])
        raw = _json_object(row["raw_value_json"])
        available_at = _aware_timestamp(row["available_at"])
        if trade_date is None or dimensions is None or raw is None or available_at is None:
            continue
        symbol = dimensions.get("symbol")
        maturity = parse_reference_date(dimensions.get("maturity"))
        rate = _finite_number(raw.get("rate"))
        if not isinstance(symbol, str) or not symbol or maturity is None or rate is None:
            continue
        key = (trade_date, symbol, maturity)
        latest[key] = DIContract(
            trade_date=trade_date,
            symbol=symbol,
            maturity=maturity,
            rate=rate,
            available_at=available_at,
            availability_basis=_availability_basis(row),
        )
    return tuple(latest[key] for key in sorted(latest))
