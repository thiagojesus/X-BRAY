"""Bounded, atomic persistence for complete Tesouro Direto CSV cohorts."""

from __future__ import annotations

import hashlib
import json
import re
from collections.abc import Callable
from datetime import date, datetime, timezone
from decimal import Decimal, InvalidOperation
from time import monotonic
from typing import TypedDict

from datafetchers.tesouro_bonds import bond_code
from db import store
from db.store import TreasuryCohortIntent, TreasuryCohortReceipt, commit_treasury_cohort

TREASURY_TOTAL_BUDGET_SECONDS = 90.0
TREASURY_NETWORK_BUDGET_SECONDS = 60.0
TREASURY_POST_NETWORK_RESERVE_SECONDS = 25.0
TREASURY_OUTCOME_RESERVE_SECONDS = 5.0
TREASURY_REQUIRED_HEADERS = frozenset({
    "Tipo Titulo",
    "Data Vencimento",
    "Data Base",
    "Taxa Compra Manha",
    "Taxa Venda Manha",
    "PU Compra Manha",
    "PU Venda Manha",
    "PU Base Manha",
})
_NUMBER_PATTERN = re.compile(
    r"^[+-]?(?:(?:\d{1,3}(?:\.\d{3})+|\d+)(?:,\d+)?|\d+(?:\.\d+)?)$"
)


class TreasuryCsvError(ValueError):
    """The downloaded CSV cannot represent a complete valid official cohort."""


class TreasuryCsvRow(TypedDict):
    values: dict[str, str]
    bond_code: int
    base_date: str


def _parse_date(value: str) -> date:
    if not re.fullmatch(r"(?:\d{4}-\d{2}-\d{2}|\d{2}/\d{2}/\d{4})", value):
        raise TreasuryCsvError("treasury CSV contains an invalid date")
    try:
        parsed = date.fromisoformat(value) if re.fullmatch(r"\d{4}-\d{2}-\d{2}", value) else date(
            int(value[6:10]), int(value[3:5]), int(value[0:2])
        )
    except (ValueError, IndexError) as exc:
        raise TreasuryCsvError("treasury CSV contains an invalid date") from exc
    return parsed


def _validate_optional_decimal(value: str) -> None:
    trimmed = value.strip()
    if not trimmed:
        return
    if not _NUMBER_PATTERN.fullmatch(trimmed):
        raise TreasuryCsvError("treasury CSV contains an invalid financial value")
    normalized = trimmed.replace(".", "").replace(",", ".") if "," in trimmed else trimmed
    try:
        number = Decimal(normalized)
    except InvalidOperation as exc:
        raise TreasuryCsvError("treasury CSV contains an invalid financial value") from exc
    if not number.is_finite():
        raise TreasuryCsvError("treasury CSV contains a non-finite financial value")


def validate_treasury_csv(
    payload: bytes,
    *,
    content_length: str | None = None,
    clock: Callable[[], float] = monotonic,
    deadline_at: float | None = None,
    capture_date: date | None = None,
) -> tuple[list[TreasuryCsvRow], str]:
    if deadline_at is not None and clock() >= deadline_at - TREASURY_OUTCOME_RESERVE_SECONDS:
        raise TimeoutError("treasury cohort deadline elapsed before CSV parsing")
    if not payload or payload.lstrip().startswith((b"<", b"{")):
        raise TreasuryCsvError("treasury provider response is empty or not CSV")
    if content_length is not None:
        try:
            declared = int(content_length)
        except ValueError as exc:
            raise TreasuryCsvError("treasury provider returned invalid content length") from exc
        if declared != len(payload):
            raise TreasuryCsvError("treasury CSV payload is truncated")

    import csv
    import io

    try:
        text = payload.decode("utf-8-sig")
        reader = csv.DictReader(io.StringIO(text, newline=""), delimiter=";", strict=True)
        headers = reader.fieldnames
        if headers is None or len(headers) != len(set(headers)) or not TREASURY_REQUIRED_HEADERS.issubset(headers):
            raise TreasuryCsvError("treasury CSV is missing required official columns")
        parsed: list[TreasuryCsvRow] = []
        observed_on = capture_date or datetime.now(timezone.utc).date()
        identities: set[tuple[int, str]] = set()
        for row_number, row in enumerate(reader, start=2):
            if deadline_at is not None and row_number % 1024 == 0 and clock() >= deadline_at - TREASURY_OUTCOME_RESERVE_SECONDS:
                raise TimeoutError("treasury cohort deadline elapsed while parsing CSV")
            if None in row or any(value is None for value in row.values()):
                raise TreasuryCsvError("treasury CSV contains a truncated or malformed row")
            values = {key: value for key, value in row.items() if key is not None}
            bond_type = values["Tipo Titulo"].strip()
            maturity = values["Data Vencimento"].strip()
            base_text = values["Data Base"].strip()
            if not bond_type or not maturity or not base_text:
                raise TreasuryCsvError("treasury CSV contains an incomplete bond identity")
            _parse_date(maturity)
            base_date = _parse_date(base_text)
            if base_date > observed_on:
                raise TreasuryCsvError("treasury CSV contains a future quote base date")
            for field in TREASURY_REQUIRED_HEADERS - {"Tipo Titulo", "Data Vencimento", "Data Base"}:
                _validate_optional_decimal(values[field])
            code = bond_code(bond_type, maturity)
            identity = (code, base_date.isoformat())
            if identity in identities:
                raise TreasuryCsvError("treasury CSV contains duplicate bond/date identity")
            identities.add(identity)
            parsed.append({"values": values, "bond_code": code, "base_date": base_date.isoformat()})
    except (UnicodeDecodeError, csv.Error) as exc:
        raise TreasuryCsvError("treasury provider response is not a complete CSV") from exc
    if not parsed:
        raise TreasuryCsvError("treasury CSV contains no complete cohort rows")
    return parsed, hashlib.sha256(payload).hexdigest()


def prepare_treasury_rows(
    parsed: list[TreasuryCsvRow],
    *,
    clock: Callable[[], float] = monotonic,
    deadline_at: float | None = None,
) -> list[tuple[int, str, str]]:
    rows: list[tuple[int, str, str]] = []
    for index, item in enumerate(parsed):
        if deadline_at is not None and index % 1024 == 0 and clock() >= deadline_at - TREASURY_OUTCOME_RESERVE_SECONDS:
            raise TimeoutError("treasury cohort deadline elapsed while serializing rows")
        payload = json.dumps(item["values"], ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False)
        rows.append((item["bond_code"], item["base_date"], payload))
    return rows


def persist_treasury_rows(
    rows: list[tuple[int, str, str]],
    *,
    extraction_id: str,
    source_payload_sha256: str,
    captured_at: str,
    reference_min: str,
    reference_max: str,
    deadline_at: float,
    clock: Callable[[], float] = monotonic,
    intent: TreasuryCohortIntent | None = None,
) -> TreasuryCohortReceipt:
    if not rows:
        raise TreasuryCsvError("treasury CSV contains no complete cohort rows")
    if clock() >= deadline_at - TREASURY_OUTCOME_RESERVE_SECONDS:
        raise TimeoutError("treasury cohort deadline elapsed before database persistence")
    receipt = TreasuryCohortReceipt(
        extraction_id,
        source_payload_sha256,
        len(rows),
        reference_min,
        reference_max,
        captured_at,
    )
    bound_intent = (
        None if intent is None
        else intent._replace(cohort_rows_sha256=treasury_cohort_rows_sha256(rows))
    )
    return commit_treasury_cohort(
        rows, receipt, deadline_at=deadline_at, clock=clock, intent=bound_intent
    )


def treasury_cohort_rows_sha256(rows: list[tuple[int, str, str]]) -> str:
    canonical = json.dumps(sorted(rows), separators=(",", ":"), ensure_ascii=True)
    return hashlib.sha256(canonical.encode()).hexdigest()


def active_treasury_cohort_fingerprint() -> tuple[str, int, str | None, str | None]:
    rows = store.query_rows(
        "SELECT bond_code, base_date, data_json FROM treasury ORDER BY bond_code, base_date"
    )
    canonical_rows = [
        (int(str(row["bond_code"])), str(row["base_date"]), str(row["data_json"]))
        for row in rows
    ]
    digest = treasury_cohort_rows_sha256(canonical_rows) if canonical_rows else ""
    dates = [row[1] for row in canonical_rows]
    return digest, len(canonical_rows), min(dates) if dates else None, max(dates) if dates else None
