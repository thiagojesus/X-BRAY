"""Independent bounded source probes and semantic comparisons; no ingestion calls."""

from __future__ import annotations

import csv
import hashlib
import io
import json
import re
import struct
import time
import xml.etree.ElementTree as ET
import zipfile
import zlib
from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal, InvalidOperation
from email.utils import parsedate_to_datetime
from typing import Any, TypeAlias, cast
from urllib.parse import urlparse

import httpx

from provider_http import ProviderResponseDecodingError, get_provider_response_bounded
from source_audit.catalog import (
    ANBIMA_URL,
    B3_URL,
    CATALOG_VERSION,
    FOCUS_12M,
    FOCUS_ANNUAL,
    FOCUS_ANNUAL_BASES,
    FOCUS_ANNUAL_HORIZON_OFFSETS,
    FOCUS_CONTRACT_VERSION,
    SGS_CODES,
    SIDRA_DATASETS,
    TREASURY_URL,
)

MAX_JSON_BYTES = 5 * 1024 * 1024
MAX_BINARY_BYTES = 30 * 1024 * 1024
MAX_CSV_BYTES = 64 * 1024 * 1024
PER_SOURCE_DEADLINE = 45
FOCUS_TOP = 200
SGS_FALLBACK_DAYS = 60
_B3_NS = "urn:bvmf.217.01.xsd"
SourceRow: TypeAlias = dict[str, object]
SourceDatabase: TypeAlias = dict[str, list[SourceRow]]


class InvalidTRSourcePayload(ValueError):
    """A structurally malformed or internally conflicting SGS 226 payload."""


@dataclass(frozen=True, slots=True)
class TRSourcePeriod:
    start: date
    end: date
    value: Decimal
    source_row: SourceRow


class EmptySourceResponseError(ValueError):
    """HTTP response contained no source bytes; no file validity is implied."""


def _source_object(value: object) -> SourceRow | None:
    if not isinstance(value, dict):
        return None
    result: SourceRow = {}
    for key, item in cast(dict[object, object], value).items():
        if not isinstance(key, str):
            return None
        result[key] = item
    return result


def _tr_source_date(value: object) -> date | None:
    """Parse only the BCB SGS 226 wire format DD/MM/YYYY."""
    if not isinstance(value, str) or re.fullmatch(r"[0-9]{2}/[0-9]{2}/[0-9]{4}", value) is None:
        return None
    try:
        day, month, year = (int(part) for part in value.split("/"))
        return date(year, month, day)
    except ValueError:
        return None


def _parse_tr_source_payload(payload: object) -> list[TRSourcePeriod]:
    """Validate all SGS 226 rows before temporal filtering or fallback."""
    if not isinstance(payload, list):
        raise InvalidTRSourcePayload("invalid_tr_period_payload_shape")
    periods: dict[tuple[date, date], TRSourcePeriod] = {}
    for index, item in enumerate(cast(list[object], payload)):
        row = _source_object(item)
        if row is None:
            raise InvalidTRSourcePayload(f"invalid_tr_period_row:{index}")
        start_value, end_value = row.get("data"), row.get("dataFim")
        if not isinstance(start_value, str) or not isinstance(end_value, str):
            raise InvalidTRSourcePayload(f"missing_tr_period_date:{index}")
        start, end = _tr_source_date(start_value), _tr_source_date(end_value)
        if start is None or end is None or end <= start:
            raise InvalidTRSourcePayload(f"invalid_tr_period_interval:{index}")
        value = _number(row.get("valor"))
        if value is None:
            raise InvalidTRSourcePayload(f"invalid_tr_period_value:{index}")
        identity = (start, end)
        prior = periods.get(identity)
        if prior is not None:
            if prior.value != value:
                raise InvalidTRSourcePayload(f"conflicting_tr_period_duplicate:{index}")
            continue
        periods[identity] = TRSourcePeriod(start, end, value, row)
    return list(periods.values())


def _source_rows(value: object) -> list[SourceRow]:
    """Validate an untrusted JSON array of row objects before adapter use."""
    if not isinstance(value, list):
        return []
    rows: list[SourceRow] = []
    for item in cast(list[object], value):
        row = _source_object(item)
        if row is not None:
            rows.append(row)
    return rows


def _database_snapshot(value: object) -> SourceDatabase:
    database = _source_object(value)
    if database is None:
        raise TypeError("invalid_database_snapshot")
    normalized: SourceDatabase = {}
    for name, table in database.items():
        if not isinstance(table, list):
            raise TypeError("invalid_database_table")
        table_rows = cast(list[object], table)
        rows = _source_rows(table_rows)
        if len(rows) != len(table_rows):
            raise TypeError("invalid_database_row")
        normalized[name] = rows
    return normalized


def _get(
    url: str,
    params: dict[str, str] | None,
    cap: int,
    deadline: float = PER_SOURCE_DEADLINE,
    *,
    headers: dict[str, str] | None = None,
    capture_decode_context: bool = False,
    max_attempts: int = 2,
    max_retry_wait_seconds: float = 2.0,
    expected_hosts: frozenset[str] | None = None,
) -> bytes:
    request_headers = {"Accept": "application/json, text/csv, application/vnd.ms-excel, application/zip, */*"}
    request_headers.update(headers or {})
    response = get_provider_response_bounded(
        url, params=params, max_bytes=cap, deadline_seconds=deadline,
        headers=request_headers, max_attempts=max_attempts, max_retry_wait_seconds=max_retry_wait_seconds,
        capture_decode_context=capture_decode_context,
    )
    if expected_hosts is not None:
        final_host = (urlparse(str(response.request.url)).hostname or "").lower()
        if final_host not in expected_hosts:
            raise ValueError("provider_redirect_host_rejected")
    return response.content


def _json(
    url: str,
    params: dict[str, str] | None = None,
    *,
    headers: dict[str, str] | None = None,
    capture_decode_context: bool = False,
    deadline: float = PER_SOURCE_DEADLINE,
) -> object:
    body = _get(
        url, params, MAX_JSON_BYTES,
        headers=headers, capture_decode_context=capture_decode_context, deadline=deadline,
    )
    try:
        return cast(object, json.loads(body))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ValueError("invalid_source_json") from exc


def _date(value: Any) -> date | None:
    if not isinstance(value, str):
        return None
    for candidate, pattern in ((value, None), (value[:10], None)):
        try:
            return date.fromisoformat(candidate)
        except ValueError:
            pass
    for pattern in ("%d/%m/%Y", "%Y%m", "%Y%m%d", "%Y"):
        try:
            parsed = datetime.strptime(value, pattern).replace(tzinfo=timezone.utc).date()
            return parsed.replace(day=1) if pattern == "%Y%m" else parsed.replace(month=1, day=1) if pattern == "%Y" else parsed
        except ValueError:
            continue
    return None


def _as_of_utc(value: str, fallback: datetime | None = None) -> datetime:
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        parsed = fallback or datetime.now(timezone.utc)
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def _number(value: Any) -> Decimal | None:
    if isinstance(value, bool) or value is None:
        return None
    try:
        result = Decimal(str(value).replace(",", "."))
    except (InvalidOperation, ValueError):
        return None
    return result if result.is_finite() else None


def _real32(value: Decimal) -> float | None:
    try:
        return struct.unpack("!f", struct.pack("!f", float(value)))[0]
    except (OverflowError, struct.error):
        return None


def _string_field(row: SourceRow, name: str) -> str | None:
    value = row.get(name)
    return value if isinstance(value, str) else None


def _count_value(value: object) -> int:
    if isinstance(value, bool):
        return 0
    if isinstance(value, int):
        return max(value, 0)
    if isinstance(value, str):
        try:
            return max(int(value), 0)
        except ValueError:
            return 0
    return 0


def _focus_coverage(rows: list[dict[str, Any]], as_of: str, database: SourceDatabase) -> dict[str, Any]:
    parsed_as_of = _as_of_utc(as_of)
    as_of_year = parsed_as_of.year
    annual_keys = tuple(FOCUS_ANNUAL)
    years = tuple(str(as_of_year + offset) for offset in FOCUS_ANNUAL_HORIZON_OFFSETS)
    required_by_series: dict[str, set[tuple[str, str]]] = {
        key: {
            (year, str(base))
            for year in years for base in FOCUS_ANNUAL_BASES
        }
        for key in annual_keys
    }
    seen_by_series: dict[str, set[tuple[str, str]]] = {key: set() for key in annual_keys}
    rolling_count = 0
    unexpected: set[str] = set()
    source_identities: set[str] = set()
    for row in rows:
        identity = row.get("identity")
        if not isinstance(identity, str):
            continue
        pieces = identity.split(":", 2)
        if len(pieces) != 3:
            continue
        series_key, raw_dimensions = pieces[1], pieces[2]
        dimensions = raw_dimensions.split("|")
        source_identities.add(identity)
        if series_key in required_by_series and len(dimensions) == 3:
            dimension = (dimensions[1], dimensions[2])
            if dimension in required_by_series[series_key] and dimensions[0] == FOCUS_ANNUAL[series_key]:
                seen_by_series[series_key].add(dimension)
            else:
                unexpected.add(identity)
        elif series_key == FOCUS_12M and len(dimensions) == 4:
            rolling_count += 1
        else:
            unexpected.add(identity)
    missing_source: dict[str, list[tuple[str, ...]]] = {}
    missing: dict[str, list[tuple[str, ...]]] = {}
    annual_required_ids: set[str] = set()
    for key, required in required_by_series.items():
        absent = sorted(required - seen_by_series[key])
        if absent:
            missing[key] = absent
            missing_source[key] = absent
        for target, base in required:
            annual_required_ids.add(f"focus:{key}:{FOCUS_ANNUAL[key]}|{target}|{base}")
    rolling_ids = {
        identity for row in rows
        if (identity := row.get("identity"))
        and isinstance(identity, str)
        and identity.startswith(f"focus:{FOCUS_12M}:IPCA|")
        and identity.endswith("|0|S")
    }
    rolling_identity_label = f"focus:{FOCUS_12M}:<rolling_12m_current>"
    if not rolling_ids:
        missing_source[FOCUS_12M] = [("rolling_12m", "0", "S")]
    database_ids: set[str] = set()
    database_rolling_ids: set[str] = set()
    for database_key, field in (("focus", "indicator"), ("focus_vintages", "series_key")):
        for db_row in database.get(database_key, []):
            payload_field = "data_json" if database_key == "focus" else "raw_value_json"
            payload = _source_object(db_row.get(payload_field))
            survey_date = _date(payload.get("Data")) if payload else None
            if (
                payload is None or survey_date is None
                or survey_date > parsed_as_of.date()
                or _number(payload.get("Mediana")) is None
            ):
                continue
            series_key = str(db_row.get(field, ""))
            dimensions = _focus_key(payload)
            if series_key in FOCUS_ANNUAL and dimensions[0] == FOCUS_ANNUAL[series_key]:
                database_ids.add(f"focus:{series_key}:{'|'.join(dimensions[:3])}")
            elif series_key == FOCUS_12M and dimensions[0] == "IPCA" and dimensions[2:] == ("0", "S"):
                database_rolling_ids.add(f"focus:{series_key}:{'|'.join(dimensions)}")
    required_count = len(annual_keys) * len(years) * len(FOCUS_ANNUAL_BASES) + 1
    checked = sum(len(values) for values in seen_by_series.values()) + min(rolling_count, 1)
    missing_source_ids = sorted(annual_required_ids - source_identities)
    if not rolling_ids:
        missing_source_ids.append(rolling_identity_label)
    missing_database_ids = sorted(annual_required_ids - database_ids)
    missing_database_rolling_ids = sorted(rolling_ids - database_rolling_ids)
    missing_database_ids.extend(missing_database_rolling_ids)
    if not rolling_ids and not database_rolling_ids:
        missing_database_ids.append(rolling_identity_label)
    expected_rolling_identity = sorted(rolling_ids)[:1] or [rolling_identity_label]
    return {
        "complete": not missing_source_ids and not missing_database_ids and rolling_count == 1,
        "contract_version": FOCUS_CONTRACT_VERSION,
        "as_of_year": as_of_year,
        "expected_identities": required_count,
        "checked_identities": checked,
        "missing_dimensions_by_series": missing,
        "missing_source_dimensions_by_series": missing_source,
        "missing_source_identities": missing_source_ids,
        "missing_database_identities": missing_database_ids,
        "expected_identity_ids": sorted(annual_required_ids) + expected_rolling_identity,
        "unexpected_identities": sorted(unexpected),
        "rolling_12m_checked": rolling_count,
    }


def _production_snapshot_host_verified(value: object) -> bool:
    if not isinstance(value, dict):
        return False
    metadata = cast(dict[object, object], value)
    return metadata.get("matches_expected_production_host") is True and metadata.get(
        "api_observed_matches_expected_host",
    ) is True


def _valid_latest(records: list[dict[str, Any]], date_key: str, value_key: str) -> dict[str, Any] | None:
    valid = [record for record in records if _date(record.get(date_key)) is not None and _number(record.get(value_key)) is not None]
    return max(valid, key=lambda row: _date(row[date_key]) or date.min, default=None)


def _valid_on_or_before(
    records: list[SourceRow], date_key: str, value_key: str, cutoff: date,
) -> list[SourceRow]:
    return [
        row for row in records
        if (parsed := _date(row.get(date_key))) is not None
        and parsed <= cutoff and _number(row.get(value_key)) is not None
    ]


def _unavailable_result(identity: str, checked_at: str, exc: Exception) -> dict[str, Any]:
    result: dict[str, Any] = {
        "identity": identity, "status": "source_unavailable",
        "error_code": type(exc).__name__, "source_checked_at": checked_at,
    }
    if isinstance(exc, httpx.HTTPStatusError) and exc.response.status_code == 429:
        result["error_code"] = "source_rate_limited"
        raw_retry_after = exc.response.headers.get("retry-after")
        if raw_retry_after:
            delay = _number(raw_retry_after)
            if delay is None:
                try:
                    retry_at = parsedate_to_datetime(raw_retry_after)
                except (TypeError, ValueError, OverflowError):
                    result["retry_after_available"] = True
                else:
                    if retry_at.tzinfo is None:
                        retry_at = retry_at.replace(tzinfo=timezone.utc)
                    delay = Decimal(str((retry_at - datetime.now(timezone.utc)).total_seconds()))
            if delay is not None:
                result["retry_after_seconds"] = max(0, int(delay))
    if isinstance(exc, ProviderResponseDecodingError):
        result["error_code"] = "provider_response_decoding_error"
        result["http_status"] = exc.status_code
        result["content_encoding"] = exc.content_encoding
        result["request_accept_encoding"] = exc.accept_encoding
    return result


def compare_observation(
    source_ref: str | None,
    source_value: Any,
    db_ref: str | None,
    db_value: Any,
    *,
    source_checked_at: str,
    db_fetched_at: str | None = None,
    identity: str,
) -> dict[str, Any]:
    """Compare exact references and IEEE-754 REAL32 precision without UI rounding."""
    base: dict[str, Any] = {
        "identity": identity,
        "source_reference": source_ref,
        "source_value": str(source_value) if source_value is not None else None,
        "database_reference": db_ref,
        "database_value": db_value,
        "database_fetched_at": db_fetched_at,
        "source_checked_at": source_checked_at,
    }
    if db_ref is None:
        base["status"] = "database_missing"
        return base
    if source_ref is None:
        base["status"] = "source_unavailable"
        return base
    if _date(source_ref) is not None and _date(db_ref) is not None:
        source_period, db_period = _date(source_ref), _date(db_ref)
        assert source_period is not None and db_period is not None
        if source_period > db_period:
            base["status"] = "source_newer"
            return base
        if source_period < db_period:
            base["status"] = "database_ahead"
            return base
    source_number, db_number = _number(source_value), _number(db_value)
    if source_number is None or db_number is None:
        base["status"] = "invalid_source"
        return base
    db_real32 = _real32(db_number)
    source_real32 = _real32(source_number)
    if db_real32 is None or source_real32 is None:
        base["status"] = "invalid_source"
        return base
    base["status"] = "matched" if db_real32 == source_real32 else "same_reference_value_mismatch"
    return base


def _db_sgs(rows: list[dict[str, Any]]) -> dict[int, dict[str, Any]]:
    return {int(row["series_code"]): row for row in rows if row.get("series_code") is not None}


def _tr_period_map(rows: list[SourceRow]) -> dict[str, str] | None:
    """Return an exact end-date/value cohort, rejecting ambiguous revisions."""
    periods: dict[str, str] = {}
    for row in rows:
        period_end = _date(row.get("dataFim"))
        value = _number(row.get("valor"))
        if period_end is None or value is None:
            return None
        normalized_value = format(value.normalize(), "f")
        end_key = period_end.isoformat()
        if end_key in periods and periods[end_key] != normalized_value:
            return None
        periods[end_key] = normalized_value
    return periods


def _probe_tr_sgs(
    database: SourceDatabase,
    checked_at: str,
    cutoff: date,
    deadline: float,
    initial_payload: object,
) -> dict[str, Any]:
    """Audit SGS 226 period identities, validating each raw response in place."""
    parsed_source = _parse_tr_source_payload(initial_payload)
    future_starts = [period for period in parsed_source if period.start > cutoff]
    eligible = [period for period in parsed_source if period.start <= cutoff]
    used_fallback = not eligible
    if used_fallback:
        window_start = cutoff - timedelta(days=SGS_FALLBACK_DAYS)
        history = _json(
            "https://api.bcb.gov.br/dados/serie/bcdata.sgs.226/dados",
            {"formato": "json", "dataInicial": window_start.strftime("%d/%m/%Y"),
             "dataFinal": cutoff.strftime("%d/%m/%Y")},
            deadline=max(0.001, deadline - time.monotonic()),
        )
        eligible = [
            period for period in _parse_tr_source_payload(history)
            if window_start <= period.start <= cutoff
        ]
    if not eligible:
        result: dict[str, Any] = {
            "identity": "bcb_sgs:226", "status": "source_anomaly_inconclusive",
            "error_code": "no_eligible_published_period", "source_reference": None,
            "database_reference": None, "source_checked_at": checked_at,
            "source_url": "https://api.bcb.gov.br/dados/serie/bcdata.sgs.226/dados/ultimos/50?formato=json",
            "source_start_cutoff": cutoff.isoformat(),
        }
        if future_starts:
            result["warning_code"] = "future_tr_period_starts_excluded"
            result["source_future_start_count"] = len(future_starts)
            result["source_latest_future_start"] = max(p.start for p in future_starts).isoformat()
        return result
    latest_start = max(period.start for period in eligible)
    candidate = [period for period in eligible if period.start == latest_start]
    exact_payload = _json(
        "https://api.bcb.gov.br/dados/serie/bcdata.sgs.226/dados",
        {"formato": "json", "dataInicial": latest_start.strftime("%d/%m/%Y"),
         "dataFinal": latest_start.strftime("%d/%m/%Y")},
        deadline=max(0.001, deadline - time.monotonic()),
    )
    exact = [p for p in _parse_tr_source_payload(exact_payload) if p.start == latest_start]
    identity = "bcb_sgs:226"
    if not exact:
        return {
            "identity": identity, "status": "source_anomaly_inconclusive",
            "error_code": "tr_period_cohort_missing_or_changed_during_audit",
            "source_reference": latest_start.isoformat(), "source_candidate_period_count": len(candidate),
            "source_checked_at": checked_at,
        }
    candidate_rows = [p.source_row for p in candidate]
    exact_rows = [p.source_row for p in exact]
    candidate_map, exact_map = _tr_period_map(candidate_rows), _tr_period_map(exact_rows)
    if candidate_map is None or exact_map is None:
        return {"identity": identity, "status": "invalid_source",
                "error_code": "conflicting_or_invalid_tr_period_identity", "source_checked_at": checked_at}
    if candidate_map != exact_map:
        return {"identity": identity, "status": "source_cohort_disagreement",
                "comparison_status": "source_cohort_disagreement", "error_code": "tr_period_cohort_changed_during_audit",
                "source_reference": latest_start.isoformat(), "source_candidate_periods": sorted(candidate_map.items()),
                "source_periods": sorted(exact_map.items()), "source_checked_at": checked_at}
    db_cohort = [r for r in database.get("tr_periods", []) if _date(r.get("period_start")) == latest_start]
    database_map = {
        (_date(r.get("period_end")) or date.min).isoformat():
        format((_number(r.get("value")) or Decimal(0)).normalize(), "f") for r in db_cohort
    }
    result = compare_observation(
        latest_start.isoformat(), len(exact_rows), latest_start.isoformat() if db_cohort else None,
        len(db_cohort), source_checked_at=checked_at,
        db_fetched_at=max((str(r.get("first_seen_at", "")) for r in db_cohort), default=None),
        identity=identity,
    )
    if exact_map != database_map:
        result["status"] = "source_contract_disagreement" if db_cohort else "database_missing"
        result["comparison_status"] = result["status"]
        result["error_code"] = "tr_period_identity_mismatch"
    result.update({
        "source_periods": sorted(exact_map.items()), "database_periods": sorted(database_map.items()),
        "source_reference": latest_start.isoformat(),
        "source_url": "https://api.bcb.gov.br/dados/serie/bcdata.sgs.226/dados/ultimos/50?formato=json",
        "source_start_cutoff": cutoff.isoformat(),
        "source_latest_period_end": max(p.end for p in exact).isoformat(),
    })
    if used_fallback:
        result["warning_code"] = "bounded_tr_history_fallback"
    if future_starts:
        result["source_future_start_count"] = len(future_starts)
        result["source_latest_future_start"] = max(p.start for p in future_starts).isoformat()
    return result


def _probe_sgs(
    database: SourceDatabase,
    checked_at: str,
    cutoff_day: date | None = None,
) -> list[dict[str, Any]]:
    db_rows = _db_sgs(database.get("sgs", []))
    cutoff = cutoff_day or _date(checked_at) or datetime.now(timezone.utc).date()
    results: list[dict[str, Any]] = []
    for code in SGS_CODES:
        try:
            probe_deadline = time.monotonic() + PER_SOURCE_DEADLINE
            initial_url = f"https://api.bcb.gov.br/dados/serie/bcdata.sgs.{code}/dados/ultimos/{50 if code == 226 else 5}"
            initial_params = {"formato": "json"}
            if code == 226:
                data = _json(
                    initial_url,
                    initial_params,
                    deadline=max(0.001, probe_deadline - time.monotonic()),
                )
            else:
                data = _json(initial_url, initial_params)
            if code == 226:
                results.append(_probe_tr_sgs(database, checked_at, cutoff, probe_deadline, data))
                continue
            if not isinstance(data, list):
                raise TypeError("invalid_source_shape")
            source_payload = cast(list[object], data)
            source_records = _source_rows(source_payload)
            if len(source_records) != len(source_payload):
                raise TypeError("invalid_source_row")
            future = [
                row for row in source_records
                if (parsed := _date(row.get("data"))) is not None and parsed > cutoff
            ]
            eligible = _valid_on_or_before(source_records, "data", "valor", cutoff)
            latest = _valid_latest(eligible, "data", "valor")
            if future and latest is None:
                start = cutoff - timedelta(days=SGS_FALLBACK_DAYS)
                history_url = f"https://api.bcb.gov.br/dados/serie/bcdata.sgs.{code}/dados"
                history = _json(history_url, {
                    "formato": "json", "dataInicial": start.strftime("%d/%m/%Y"),
                    "dataFinal": cutoff.strftime("%d/%m/%Y"),
                })
                if not isinstance(history, list):
                    raise TypeError("invalid_source_shape")
                history_payload = cast(list[object], history)
                history_records = _source_rows(history_payload)
                if len(history_records) != len(history_payload):
                    raise TypeError("invalid_source_row")
                eligible = _valid_on_or_before(history_records, "data", "valor", cutoff)
                latest = _valid_latest(eligible, "data", "valor")
            db = db_rows.get(code, {})
            result = compare_observation(
                latest.get("data") if latest else None,
                latest.get("valor") if latest else None,
                db.get("date"), db.get("value"), source_checked_at=checked_at,
                db_fetched_at=db.get("fetched_at"), identity=f"bcb_sgs:{code}",
            )
            result["source_url"] = f"https://api.bcb.gov.br/dados/serie/bcdata.sgs.{code}/dados/ultimos/5?formato=json"
            if future:
                result["source_future_observation_count"] = len(future)
                future_dates = [
                    parsed for row in future if (parsed := _date(row.get("data"))) is not None
                ]
                result["source_latest_future_date"] = max(future_dates, default=date.min).isoformat()
                result["warning_code"] = "future_source_observations_excluded"
                if latest is not None:
                    result["comparison_status"] = result["status"]
            if latest is None and future:
                result["status"] = "source_anomaly_inconclusive"
                result["error_code"] = "future_source_observations_without_valid_history"
            if latest is None and data and not future:
                result["status"] = "invalid_source"
                result["error_code"] = "no_valid_source_observation"
            results.append(result)
        except InvalidTRSourcePayload as exc:
            results.append({
                "identity": f"bcb_sgs:{code}",
                "status": "invalid_source",
                "error_code": str(exc).split(":", maxsplit=1)[0],
                "source_checked_at": checked_at,
            })
        except (httpx.HTTPError, TimeoutError, OSError) as exc:
            results.append(_unavailable_result(f"bcb_sgs:{code}", checked_at, exc))
        except (TypeError, ValueError) as exc:
            results.append({
                "identity": f"bcb_sgs:{code}",
                "status": "invalid_source",
                "error_code": type(exc).__name__,
                "source_checked_at": checked_at,
            })
        except Exception as exc:  # noqa: BLE001 - unexpected adapter faults must not imply provider outage.
            results.append({
                "identity": f"bcb_sgs:{code}",
                "status": "auditor_internal_error",
                "error_code": type(exc).__name__,
                "source_checked_at": checked_at,
            })
    return results


def _focus_key(record: dict[str, Any]) -> tuple[str, str, str, str]:
    return (
        str(record.get("Indicador", "")), str(record.get("DataReferencia", "")),
        str(record.get("baseCalculo", "")), str(record.get("Suavizada", "")),
    )


def _probe_focus(
    database: SourceDatabase, checked_at: str, database_as_of: str | None = None,
) -> list[dict[str, Any]]:
    cutoff = _as_of_utc(database_as_of or checked_at).date()
    db_rows: dict[str, list[dict[str, Any]]] = {}
    for row in database.get("focus_vintages", []):
        payload = _source_object(row.get("raw_value_json"))
        if payload is not None:
            db_rows.setdefault(str(row.get("series_key")), []).append({**payload, "_fetched_at": row.get("available_at")})
    current_rows: dict[str, list[dict[str, Any]]] = {}
    for row in database.get("focus", []):
        payload = _source_object(row.get("data_json"))
        if payload is not None:
            current_rows.setdefault(str(row.get("indicator")), []).append({
                **payload, "_fetched_at": row.get("fetched_at"),
            })
    expected: list[tuple[str, str, str, dict[str, str]]] = [
        (key, "ExpectativasMercadoAnuais", value, {"$filter": f"Indicador eq '{value}'", "$orderby": "Data desc", "$top": str(FOCUS_TOP), "$format": "json"})
        for key, value in FOCUS_ANNUAL.items()
    ]
    expected.append((FOCUS_12M, "ExpectativasMercadoInflacao12Meses", "IPCA", {
        "$filter": "Indicador eq 'IPCA' and Suavizada eq 'S' and baseCalculo eq 0",
        "$orderby": "Data desc", "$top": str(FOCUS_TOP), "$format": "json",
    }))
    results: list[dict[str, Any]] = []
    for key, endpoint, indicator, params in expected:
        try:
            data = _json(f"https://olinda.bcb.gov.br/olinda/servico/Expectativas/versao/v1/odata/{endpoint}", params)
            data_obj = _source_object(data)
            records = data_obj.get("value") if data_obj else None
            if not isinstance(records, list):
                raise TypeError("invalid_source_shape")
            typed_records = _source_rows(cast(list[object], records))
            filtered = [
                r for r in typed_records
                if r.get("Indicador") == indicator
                and (parsed := _date(r.get("Data"))) is not None and parsed <= cutoff
                and _number(r.get("Mediana")) is not None
            ]
            if key == FOCUS_12M:
                filtered = [r for r in filtered if r.get("Suavizada") == "S" and r.get("baseCalculo") == 0]
                latest_rolling = _latest_rolling_focus_record(filtered)
                filtered = [latest_rolling] if latest_rolling is not None else []
            current: dict[tuple[str, str, str, str], dict[str, Any]] = {}
            for row in filtered:
                k = _focus_key(row)
                if key in FOCUS_ANNUAL:
                    k = (*k[:3], "")
                if k not in current or str(row.get("Data", "")) > str(current[k].get("Data", "")):
                    current[k] = row
            annual_identity = key in FOCUS_ANNUAL
            current_saved = _latest_focus_identity_rows(current_rows.get(key, []), cutoff, annual_identity)
            vintage_saved = _latest_focus_identity_rows(db_rows.get(key, []), cutoff, annual_identity)
            if not current:
                results.append({"identity": f"focus:{key}", "status": "invalid_source", "error_code": "empty_valid_observation_set", "source_checked_at": checked_at})
                continue
            for k, row in current.items():
                current_row = current_saved.get(k)
                vintage_row = vintage_saved.get(k)
                saved_row = vintage_row or current_row
                row_basis = "focus_vintages" if vintage_row else "focus_current"
                result = compare_observation(
                    str(row.get("Data")) if _date(row.get("Data")) else None,
                    row.get("Mediana"),
                    str(saved_row.get("Data")) if saved_row and _date(saved_row.get("Data")) else None,
                    saved_row.get("Mediana") if saved_row else None,
                    source_checked_at=checked_at,
                    db_fetched_at=_string_field(saved_row, "_fetched_at") if saved_row else None,
                    identity=f"focus:{key}:{'|'.join(k[:3] if key in FOCUS_ANNUAL else k)}",
                )
                result["source_target"] = row.get("DataReferencia")
                vintage_row = vintage_saved.get(k)
                result["comparison_basis"] = row_basis
                result["database_storage_divergence"] = bool(
                    current_row and vintage_row and (
                        str(current_row.get("Data")) != str(vintage_row.get("Data"))
                        or _number(current_row.get("Mediana")) != _number(vintage_row.get("Mediana"))
                    )
                )
                if vintage_row:
                    result["database_vintage_reference"] = vintage_row.get("Data")
                    result["database_vintage_value"] = vintage_row.get("Mediana")
                if saved_row is None:
                    result["status"] = "database_missing"
                results.append(result)
        except Exception as exc:  # noqa: BLE001 - isolate failures per independently probed source.
            results.append(_unavailable_result(f"focus:{key}", checked_at, exc))
    return results


def _latest_focus_identity_rows(
    rows: list[SourceRow], cutoff: date, annual_identity: bool = False,
) -> dict[tuple[str, str, str, str], SourceRow]:
    selected: dict[tuple[str, str, str, str], SourceRow] = {}
    for row in rows:
        survey_date = _date(row.get("Data"))
        if survey_date is None or survey_date > cutoff or _number(row.get("Mediana")) is None:
            continue
        identity = _focus_key(row)
        if annual_identity:
            identity = (*identity[:3], "")
        current = selected.get(identity)
        current_date = _date(current.get("Data")) if current else None
        if current is None or survey_date > (current_date or date.min):
            selected[identity] = row
    return selected


def _latest_rolling_focus_record(records: list[SourceRow]) -> SourceRow | None:
    if not records:
        return None
    return max(
        records,
        key=lambda row: (
            _date(row.get("Data")) or date.min,
            str(row.get("DataReferencia") or ""),
            json.dumps(row, ensure_ascii=False, sort_keys=True, separators=(",", ":")),
        ),
    )


@dataclass(frozen=True, slots=True)
class _SidraSelection:
    observations: list[dict[str, Any]]
    latest_source_period: str | None
    unavailable_newer_rows: int
    unavailable_rows: int


def _sidra_rows(data: object, expected: dict[str, object]) -> _SidraSelection:
    if not isinstance(data, list) or not data:
        raise ValueError("invalid_source_shape")
    rows = _source_rows(cast(list[object], data))
    in_scope: list[tuple[date, str, SourceRow, object, bool]] = []
    for row in rows:
        period = row.get("D3C")
        value = row.get("V")
        # Filter geography and series dimensions before comparing periods. SIDRA
        # includes a header row and may return other dimensions in the same body.
        if str(row.get("D1C", "")) != str(expected["territory_code"]):
            continue
        variable_id = expected.get("variable_id")
        if variable_id is not None and str(row.get("D2C")) != str(variable_id):
            continue
        unit_code = expected.get("unit_code")
        if unit_code is not None and str(row.get("MC")) != str(unit_code):
            continue
        category_id = expected.get("category_id")
        if category_id is not None and str(row.get("D4C")) != str(category_id):
            continue
        parsed_period = _date(str(period)) if period is not None else None
        if parsed_period is None:
            continue
        # A dash is a published zero. Explicit unavailable markers remain
        # diagnostics so an older valid period cannot yield a false healthy run.
        if value == "-":
            value = "0"
        valid = value not in {"X", "...", "..", ".", "", None} and _number(value) is not None
        in_scope.append((parsed_period, str(period), row, value, valid))

    if not in_scope:
        return _SidraSelection([], None, 0, 0)
    latest_source_period = max(in_scope, key=lambda item: item[0])[1]
    valid_rows = [item for item in in_scope if item[4]]
    if not valid_rows:
        return _SidraSelection([], latest_source_period, len(in_scope), len(in_scope))
    latest_valid_date = max(item[0] for item in valid_rows)
    latest_valid = [item for item in valid_rows if item[0] == latest_valid_date]
    unavailable_newer_rows = sum(
        not valid and period_date > latest_valid_date
        for period_date, _period, _row, _value, valid in in_scope
    )
    observations = [
        {
            "reference_period": period,
            "value": value,
            "unit_code": row.get("MC"),
            "row": row,
            "latest_source_period": latest_source_period,
            "unavailable_newer_rows": unavailable_newer_rows,
        }
        for _period_date, period, row, value, _valid in latest_valid
    ]
    return _SidraSelection(
        observations,
        latest_source_period,
        unavailable_newer_rows,
        sum(not item[4] for item in in_scope),
    )


def _probe_sidra(database: SourceDatabase, checked_at: str) -> list[dict[str, Any]]:
    saved = {str(row.get("dataset_key")): row for row in database.get("sidra", [])}
    results: list[dict[str, Any]] = []
    for spec in SIDRA_DATASETS.values():
        try:
            url = f"https://apisidra.ibge.gov.br/values/t/{spec.table_id}/n1/{spec.territory_code}/v/{spec.variable_id}/p/last"
            if spec.classification_id is not None and spec.category_id is not None:
                url += f"/c{spec.classification_id}/{spec.category_id}"
            data = _json(
                url, {"formato": "json"}, headers={"Accept-Encoding": "identity"},
                capture_decode_context=True,
            )
            expectations: dict[str, object] = {
                "territory_code": spec.territory_code,
                "unit_code": spec.unit_code,
                "category_id": spec.category_id,
                "variable_id": spec.variable_id,
            }
            selection = _sidra_rows(data, expectations)
            if not selection.observations or selection.unavailable_newer_rows:
                fallback = f"https://apisidra.ibge.gov.br/values/t/{spec.table_id}/n1/{spec.territory_code}/v/{spec.variable_id}/p/last%205"
                if spec.classification_id is not None and spec.category_id is not None:
                    fallback += f"/c{spec.classification_id}/{spec.category_id}"
                fallback_selection = _sidra_rows(_json(
                    fallback, {"formato": "json"}, headers={"Accept-Encoding": "identity"},
                    capture_decode_context=True,
                ), expectations)
                if fallback_selection.observations or fallback_selection.latest_source_period:
                    if selection.unavailable_newer_rows:
                        observed_periods = [
                            period for period in (selection.latest_source_period, fallback_selection.latest_source_period)
                            if period is not None
                        ]
                        latest_period = max(observed_periods, key=lambda period: _date(period) or date.min, default=None)
                        selection = _SidraSelection(
                            fallback_selection.observations,
                            latest_period,
                            max(selection.unavailable_newer_rows, fallback_selection.unavailable_newer_rows),
                            max(selection.unavailable_rows, fallback_selection.unavailable_rows),
                        )
                    else:
                        selection = fallback_selection
            observations = selection.observations
            latest = max(observations, key=lambda row: _date(row["reference_period"]) or date.min, default=None)
            db = saved.get(spec.key, {})
            result = compare_observation(
                latest.get("reference_period") if latest else None,
                latest.get("value") if latest else None,
                _string_field(db, "reference_period"), db.get("value"), source_checked_at=checked_at,
                db_fetched_at=_string_field(db, "available_at"), identity=f"ibge_sidra:{spec.key}",
            )
            if latest is None:
                result["status"] = "invalid_source"
                result["error_code"] = "no_valid_sidra_observation"
                result["source_latest_period"] = selection.latest_source_period
                result["source_unavailable_rows"] = selection.unavailable_rows
            elif selection.unavailable_newer_rows:
                result["status"] = "invalid_source"
                result["error_code"] = "newer_sidra_observation_unavailable"
                result["source_latest_period"] = selection.latest_source_period
                result["source_unavailable_rows"] = selection.unavailable_rows
                result["source_unavailable_newer_rows"] = selection.unavailable_newer_rows
            elif db and (db.get("unit") != spec.unit or str(latest.get("unit_code")) != spec.unit_code):
                result["status"] = "invalid_source"
                result["error_code"] = "database_unit_mismatch"
            results.append(result)
        except Exception as exc:  # noqa: BLE001 - isolate failures per independently probed dataset.
            results.append(_unavailable_result(f"ibge_sidra:{spec.key}", checked_at, exc))
    return results


def _probe_anbima(database: SourceDatabase, checked_at: str) -> list[dict[str, Any]]:
    try:
        from python_calamine import CalamineWorkbook

        payload = _get(ANBIMA_URL, None, MAX_BINARY_BYTES)
        workbook = CalamineWorkbook.from_filelike(io.BytesIO(payload))
        db: dict[str, list[SourceRow]] = {}
        for row in database.get("anbima", []):
            db.setdefault(str(row.get("sheet_name")), []).append(row)
        results: list[dict[str, Any]] = []
        for sheet_name in workbook.sheet_names:
            sheet = workbook.get_sheet_by_name(sheet_name).to_python()
            if not sheet:
                continue
            headers = [str(value) for value in sheet[0]]
            observations: list[tuple[date, dict[str, Any]]] = []
            for row in sheet[1:]:
                record: dict[str, Any] = {headers[i]: row[i] if i < len(row) else None for i in range(len(headers))}
                raw_date = record.get("Data de Referência") or record.get("data")
                parsed = _date(raw_date.isoformat() if isinstance(raw_date, (date, datetime)) else str(raw_date or ""))
                if parsed:
                    observations.append((parsed, record))
            source_by_field: dict[str, tuple[date, object]] = {}
            for day, record in observations:
                for field, value in record.items():
                    if (
                        field not in {"Data de Referência", "data"}
                        and _number(value) is not None
                        and (field not in source_by_field or day > source_by_field[field][0])
                    ):
                        source_by_field[field] = (day, value)
            sheet_key = sheet_name.strip().lower().replace(" ", "_").replace("-", "_")
            db_rows = db.get(sheet_key, [])
            db_by_field: dict[str, dict[str, object]] = {}
            for dbrow in db_rows:
                db_record = _source_object(dbrow.get("data_json")) or {}
                for field, value in db_record.items():
                    if _number(value) is not None and field not in db_by_field:
                        db_by_field[field] = {"row": dbrow, "value": value}
            for field, (day, value) in source_by_field.items():
                saved = db_by_field.get(field)
                dbrow = _source_object(saved.get("row")) if saved else None
                dbrow = dbrow or {}
                db_day = _date(str(dbrow.get("date") or ""))
                results.append(compare_observation(
                    day.isoformat(), value,
                    db_day.isoformat() if db_day else None,
                    saved.get("value") if saved else None,
                    source_checked_at=checked_at, db_fetched_at=_string_field(dbrow, "fetched_at"), identity=f"anbima:{sheet_name}:{field}",
                ))
            if not source_by_field:
                results.append({"identity": f"anbima:{sheet_name}", "status": "invalid_source", "error_code": "no_valid_numeric_series", "source_checked_at": checked_at})
        return results or [{"identity": "anbima:IMA-B", "status": "invalid_source", "error_code": "no_usable_sheet", "source_checked_at": checked_at}]
    except Exception as exc:  # noqa: BLE001 - report a typed provider failure without suppressing the audit.
        return [_unavailable_result("anbima:IMA-B", checked_at, exc)]


def _parse_treasury_csv(payload: bytes) -> dict[tuple[str, str], dict[str, str]]:
    text = payload.decode("utf-8-sig")
    latest: dict[tuple[str, str], tuple[date, dict[str, str]]] = {}
    for row in csv.DictReader(io.StringIO(text, newline=""), delimiter=";"):
        maturity = row.get("Data Vencimento", "").strip()
        base = row.get("Data Base", "").strip()
        parsed = _date(base)
        kind = row.get("Tipo Titulo", "").strip()
        if parsed is None or _date(maturity) is None or not kind:
            continue
        key = (kind, maturity)
        if key in latest and parsed == latest[key][0] and row != latest[key][1]:
            raise ValueError("treasury_conflicting_duplicate_observation")
        if key not in latest or parsed > latest[key][0]:
            latest[key] = (parsed, row)
    return {key: row for key, (_day, row) in latest.items()}


def _probe_cutoff(checked_at: str) -> date:
    return _as_of_utc(checked_at).date()


def _b3_probe_date(cutoff: date) -> date:
    candidate = cutoff
    while candidate.weekday() >= 5:
        candidate -= timedelta(days=1)
    return candidate


def _probe_failure(provider: str, checked_at: str, exc: Exception) -> dict[str, Any]:
    failure = _unavailable_result(provider, checked_at, exc)
    failure_class = "source_unavailable" if isinstance(exc, EmptySourceResponseError) else "transport" if isinstance(
        exc,
        (httpx.TimeoutException, httpx.NetworkError, TimeoutError, OSError, httpx.HTTPStatusError),
    ) else "source_contract"
    failure.update({
        "status": "source_unavailable" if failure_class in {"transport", "source_unavailable"} else "invalid_source",
        "failure_class": failure_class,
        "environment": None,
        "row_count": 0,
    })
    if isinstance(exc, httpx.HTTPStatusError):
        failure["http_status"] = exc.response.status_code
    return failure


def collect_provider_probe(
    provider: str,
    checked_at: str,
    *,
    environment: str,
) -> dict[str, Any]:
    """Perform one fixed, bounded public-source read with independent audit parsers."""
    started = time.monotonic()
    cutoff = _probe_cutoff(checked_at)
    if provider not in {"b3", "tesouro"}:
        raise ValueError("unsupported_source_probe")
    try:
        if provider == "b3":
            trade_day = _b3_probe_date(cutoff)
            payload = _get(
                B3_URL.format(YYMMDD=trade_day.strftime("%y%m%d")),
                None,
                MAX_BINARY_BYTES,
                deadline=60,
                max_attempts=1,
                expected_hosts=frozenset({"www.b3.com.br"}),
            )
            if not payload:
                raise EmptySourceResponseError("empty_http_200_body")
            rows = _parse_b3(payload, {})
            selected = [
                row for row in rows
                if _date(row.get("trade_date")) == trade_day
                and isinstance(row.get("security_id"), str)
                and _number(row.get("rate")) is not None
            ]
            if not selected:
                return {
                    "provider": provider, "environment": environment, "status": "invalid_source",
                    "failure_class": "source_contract", "error_code": "no_valid_di_quotes",
                    "cutoff_at": checked_at, "source_checked_at": datetime.now(timezone.utc).isoformat(),
                    "source_reference": trade_day.isoformat(),
                    "row_count": 0, "elapsed_seconds": round(time.monotonic() - started, 3),
                }
            rows = [{key: row.get(key) for key in ("symbol", "rate", "security_id", "trade_date")} for row in selected]
            reference = trade_day.isoformat()
        else:
            payload = _get(
                TREASURY_URL,
                None,
                MAX_CSV_BYTES,
                deadline=90,
                max_attempts=2,
                max_retry_wait_seconds=30,
                expected_hosts=frozenset({"www.tesourotransparente.gov.br"}),
            )
            all_rows = _parse_treasury_csv(payload)
            dated = [
                parsed for row in all_rows.values()
                if (parsed := _date(row.get("Data Base", ""))) is not None and parsed <= cutoff
            ]
            latest_base = max(dated, default=None)
            selected_rows = [
                (key, row) for key, row in all_rows.items()
                if latest_base is not None
                and _date(row.get("Data Base", "")) == latest_base
                and (_date(key[1]) or date.min) >= latest_base
                and key[0].lower().startswith(("tesouro prefixado", "tesouro ipca", "tesouro selic"))
            ]
            if not selected_rows:
                return {
                    "provider": provider, "environment": environment, "status": "invalid_source",
                    "failure_class": "source_contract", "error_code": "no_valid_bond_rows",
                    "cutoff_at": checked_at, "source_checked_at": datetime.now(timezone.utc).isoformat(),
                    "source_reference": latest_base.isoformat() if latest_base else None,
                    "row_count": 0, "elapsed_seconds": round(time.monotonic() - started, 3),
                }
            fields = {
                "buy_rate": "Taxa Compra Manha", "sell_rate": "Taxa Venda Manha",
                "buy_price": "PU Compra Manha", "sell_price": "PU Venda Manha",
                "base_price": "PU Base Manha",
            }
            rows = [{
                "kind": kind,
                "maturity": maturity,
                "base_date": row.get("Data Base"),
                **{name: row.get(source_name) for name, source_name in fields.items()},
            } for (kind, maturity), row in selected_rows]
            required_fields = tuple(fields)
            if any(_number(row.get(name)) is None for row in rows for name in required_fields):
                raise ValueError("treasury_active_row_has_invalid_numeric_value")
            reference = latest_base.isoformat() if latest_base else None
        return {
            "provider": provider,
            "environment": environment,
            "status": "valid",
            "failure_class": None,
            "cutoff_at": checked_at,
            "source_checked_at": datetime.now(timezone.utc).isoformat(),
            "source_reference": reference,
            "row_count": len(rows),
            "payload_bytes": len(payload),
            "payload_sha256": hashlib.sha256(payload).hexdigest(),
            "elapsed_seconds": round(time.monotonic() - started, 3),
            "rows": rows,
        }
    except Exception as exc:  # noqa: BLE001 - report typed bounded probe failures.
        failure = _probe_failure(provider, checked_at, exc)
        return {
            **failure,
            "provider": provider,
            "environment": environment,
            "cutoff_at": checked_at,
            "source_checked_at": datetime.now(timezone.utc).isoformat(),
            "elapsed_seconds": round(time.monotonic() - started, 3),
        }


def _probe_treasury(database: SourceDatabase, checked_at: str) -> list[dict[str, Any]]:
    try:
        source_rows = _parse_treasury_csv(_get(TREASURY_URL, None, MAX_CSV_BYTES, deadline=90))
        parsed_bases = [parsed for row in source_rows.values() if (parsed := _date(row.get("Data Base", ""))) is not None]
        latest_base = max(parsed_bases, default=None)
        today = datetime.now(timezone.utc).date()
        active_source = {
            key: row for key, row in source_rows.items()
            if latest_base is not None
            and _date(row.get("Data Base", "")) == latest_base
            and (_date(key[1]) or date.min) >= today
            and key[0].lower().startswith(("tesouro prefixado", "tesouro ipca", "tesouro selic"))
        }
        saved: list[dict[str, Any]] = []
        for row in database.get("treasury", []):
            data = row.get("data_json")
            data_record = _source_object(data)
            if data_record is not None:
                saved.append({**data_record, "_db_base_date": row.get("base_date"), "_db_fetched_at": row.get("fetched_at")})
        results: list[dict[str, Any]] = []
        for (kind, maturity), source in active_source.items():
            normalized_maturity = (_date(maturity) or date.min).isoformat()
            dbrow = next((row for row in saved if row.get("name") == source.get("Tipo Titulo") and (_date(str(row.get("maturityDate", ""))) or date.min).isoformat() == normalized_maturity), None)
            ref = _date(source.get("Data Base", ""))
            fields = (("buy_rate", "Taxa Compra Manha"), ("sell_rate", "Taxa Venda Manha"),
                      ("buy_price", "PU Compra Manha"), ("sell_price", "PU Venda Manha"), ("base_price", "PU Base Manha"))
            for db_field, source_field in fields:
                results.append(compare_observation(
                    ref.isoformat() if ref else None, source.get(source_field),
                    dbrow.get("_db_base_date") if dbrow else None,
                    dbrow.get({"buy_rate": "buyRate", "sell_rate": "sellRate", "buy_price": "buyPrice", "sell_price": "sellPrice", "base_price": "basePrice"}[db_field]) if dbrow else None,
                    source_checked_at=checked_at, db_fetched_at=dbrow.get("_db_fetched_at") if dbrow else None,
                    identity=f"tesouro:{kind}:{maturity}:{db_field}",
                ))
        return results or [{"identity": "tesouro", "status": "invalid_source", "error_code": "no_valid_bond_rows", "source_checked_at": checked_at}]
    except Exception as exc:  # noqa: BLE001 - report a typed provider failure without suppressing the audit.
        return [_unavailable_result("tesouro", checked_at, exc)]


def _parse_b3(data: bytes, identities: dict[str, dict[str, Any]]) -> list[dict[str, Any]]:
    if not data:
        raise EmptySourceResponseError("empty_http_200_body")
    with zipfile.ZipFile(io.BytesIO(data)) as archive:
        infos = archive.infolist()
        if len(infos) > 100 or sum(info.file_size for info in infos) > MAX_BINARY_BYTES * 4:
            raise ValueError("b3_archive_limits_exceeded")
        quotes: list[dict[str, Any]] = []
        for info in infos:
            if info.is_dir():
                continue
            if info.file_size > MAX_BINARY_BYTES:
                raise ValueError("b3_archive_member_limit_exceeded")
            raw = archive.read(info)
            if b"<!DOCTYPE" in raw.upper() or b"<!ENTITY" in raw.upper():
                raise ValueError("b3_xml_dtd_forbidden")
            if not info.filename.lower().endswith(".xml"):
                try:
                    with zipfile.ZipFile(io.BytesIO(raw)) as nested:
                        inner_files = nested.infolist()
                        if len(inner_files) > 100 or sum(item.file_size for item in inner_files) > MAX_BINARY_BYTES:
                            raise ValueError("b3_nested_archive_limits_exceeded")
                        for inner in inner_files:
                            if inner.filename.lower().endswith(".xml"):
                                quotes.extend(_parse_b3_xml(nested.read(inner), identities))
                except zipfile.BadZipFile:
                    continue
            else:
                quotes.extend(_parse_b3_xml(raw, identities))
    return quotes


def _parse_b3_xml(data: bytes, identities: dict[str, dict[str, Any]]) -> list[dict[str, Any]]:
    if b"<!DOCTYPE" in data.upper() or b"<!ENTITY" in data.upper():
        raise ValueError("b3_xml_dtd_forbidden")
    root = ET.fromstring(data)
    records: list[dict[str, Any]] = []
    for price in root.iter(f"{{{_B3_NS}}}PricRpt"):
        symbol_node = price.find(f"{{{_B3_NS}}}SctyId/{{{_B3_NS}}}TckrSymb")
        if symbol_node is None or not symbol_node.text or not symbol_node.text.strip().startswith("DI1"):
            continue
        symbol = symbol_node.text.strip()
        attrs = price.find(f"{{{_B3_NS}}}FinInstrmAttrbts")
        rate_node = attrs.find(f"{{{_B3_NS}}}AdjstdQtTax") if attrs is not None else None
        if rate_node is None or _number(rate_node.text) is None:
            continue
        identity = identities.get(symbol)
        id_node = price.find(f"{{{_B3_NS}}}FinInstrmId/{{{_B3_NS}}}OthrId/{{{_B3_NS}}}Id")
        if id_node is None:
            id_node = price.find(f"{{{_B3_NS}}}SctyId/{{{_B3_NS}}}ISIN")
        date_node = price.find(f"{{{_B3_NS}}}TradDt/{{{_B3_NS}}}Dt")
        records.append({
            "symbol": symbol, "rate": str(rate_node.text),
            "maturity": identity.get("maturity") if identity else None,
            "security_id": id_node.text.strip() if id_node is not None and id_node.text else None,
            "trade_date": date_node.text.strip() if date_node is not None and date_node.text else None,
        })
    return records


def _probe_b3(database: SourceDatabase, checked_at: str) -> list[dict[str, Any]]:
    saved_identity = {str(row.get("symbol")): row for row in database.get("b3_identity", [])}
    saved_quotes = {str(row.get("symbol")): row for row in database.get("b3_di", [])}
    try:
        today = datetime.now(timezone.utc).date()
        days = [today - timedelta(days=delta) for delta in range(5) if (today - timedelta(days=delta)).weekday() < 5]
        payload: bytes | None = None
        trade_day = None
        parsed: list[dict[str, Any]] = []
        for index, day in enumerate(days):
            compact = day.strftime("%y%m%d")
            try:
                payload = _get(B3_URL.format(YYMMDD=compact), None, MAX_BINARY_BYTES, deadline=30)
            except httpx.HTTPStatusError as exc:
                if exc.response.status_code != 404:
                    raise
                payload = None
            if payload:
                try:
                    parsed = _parse_b3(payload, saved_identity)
                except zipfile.BadZipFile:
                    parsed = []
                if parsed:
                    trade_day = day
                    break
            if index < len(days) - 1:
                time.sleep(30)
        if not payload or not trade_day:
            return [{"identity": "b3:di", "status": "source_unavailable", "error_code": "no_recent_official_quote_file", "source_checked_at": checked_at}]
        results: list[dict[str, Any]] = []
        for quote_row in parsed:
            symbol = quote_row["symbol"]
            db = saved_quotes.get(symbol)
            ident = saved_identity.get(symbol, {})
            identity = f"b3_di:{symbol}:{quote_row.get('maturity') or ident.get('maturity') or 'identity_unverified'}"
            if (
                quote_row.get("maturity") is None
                or not quote_row.get("security_id")
                or quote_row.get("security_id") != ident.get("security_id")
            ):
                results.append({"identity": identity, "status": "unsupported_coverage", "error_code": "instrument_identity_unverified", "source_checked_at": checked_at})
                continue
            results.append(compare_observation(
                (_date(quote_row.get("trade_date")) or trade_day).isoformat(),
                quote_row["rate"], _string_field(db, "trade_date") if db else None,
                db.get("rate") if db else None, source_checked_at=checked_at,
                db_fetched_at=_string_field(db, "fetched_at") if db else None, identity=identity,
            ))
        source_symbols = {str(row["symbol"]) for row in parsed}
        for symbol, db in saved_quotes.items():
            ident = saved_identity.get(symbol, {})
            maturity = _date(str(ident.get("maturity") or db.get("maturity") or ""))
            end = _date(str(ident.get("trading_end") or ""))
            if symbol not in source_symbols and maturity and maturity >= trade_day and (end is None or end >= trade_day):
                results.append({
                    "identity": f"b3_di:{symbol}:{maturity.isoformat()}",
                    "status": "source_unavailable",
                    "error_code": "active_contract_missing_from_latest_curve",
                    "source_checked_at": checked_at,
                })
        return results or [{"identity": "b3:di", "status": "invalid_source", "error_code": "no_valid_di_quotes", "source_checked_at": checked_at}]
    except Exception as exc:  # noqa: BLE001 - report a typed provider failure without suppressing the audit.
        return [_unavailable_result("b3:di", checked_at, exc)]


def _compare_b3_probe(database: SourceDatabase, checked_at: str, probe: dict[str, Any]) -> list[dict[str, Any]]:
    if probe.get("status") != "valid" or not isinstance(probe.get("rows"), list):
        result = {
            "identity": "b3:di",
            "status": "source_unavailable" if probe.get("failure_class") == "transport" else "invalid_source",
            "error_code": probe.get("error_code") or "invalid_probe_payload",
            "source_checked_at": checked_at,
        }
        for key in ("http_status", "retry_after_seconds", "elapsed_seconds"):
            if probe.get(key) is not None:
                result[key] = probe[key]
        return [result]
    saved_identity: dict[str, SourceRow] = {str(row.get("symbol")): row for row in database.get("b3_identity", [])}
    saved_quotes: dict[str, SourceRow] = {str(row.get("symbol")): row for row in database.get("b3_di", [])}
    raw_rows = probe.get("rows")
    rows: list[SourceRow] = [
        cast(SourceRow, row) for row in cast(list[object], raw_rows) if isinstance(row, dict)
    ] if isinstance(raw_rows, list) else []
    parsed_symbols: set[str] = set()
    results: list[dict[str, Any]] = []
    trade_day = _date(probe.get("source_reference"))
    if trade_day is None:
        return [{"identity": "b3:di", "status": "invalid_source", "error_code": "invalid_probe_reference", "source_checked_at": checked_at}]
    for quote in rows:
        symbol = str(quote.get("symbol") or "")
        parsed_symbols.add(symbol)
        database_row = saved_quotes.get(symbol)
        identity_row = saved_identity.get(symbol, {})
        maturity = _date(str(identity_row.get("maturity") or ""))
        identity = f"b3_di:{symbol}:{maturity.isoformat() if maturity else 'identity_unverified'}"
        if maturity is None or not quote.get("security_id") or quote.get("security_id") != identity_row.get("security_id"):
            results.append({"identity": identity, "status": "unsupported_coverage", "error_code": "instrument_identity_unverified", "source_checked_at": checked_at})
            continue
        results.append(compare_observation(
            str(quote.get("trade_date") or trade_day.isoformat()), quote.get("rate"),
            _string_field(database_row, "trade_date") if database_row else None,
            database_row.get("rate") if database_row else None,
            source_checked_at=checked_at,
            db_fetched_at=_string_field(database_row, "fetched_at") if database_row else None,
            identity=identity,
        ))
    for symbol, database_row in saved_quotes.items():
        identity_row = saved_identity.get(symbol, {})
        maturity = _date(str(identity_row.get("maturity") or database_row.get("maturity") or ""))
        end = _date(str(identity_row.get("trading_end") or ""))
        if symbol not in parsed_symbols and maturity and maturity >= trade_day and (end is None or end >= trade_day):
            results.append({"identity": f"b3_di:{symbol}:{maturity.isoformat()}", "status": "source_unavailable", "error_code": "active_contract_missing_from_latest_curve", "source_checked_at": checked_at})
    return results or [{"identity": "b3:di", "status": "invalid_source", "error_code": "no_valid_di_quotes", "source_checked_at": checked_at}]


def _compare_treasury_probe(database: SourceDatabase, checked_at: str, probe: dict[str, Any]) -> list[dict[str, Any]]:
    if probe.get("status") != "valid" or not isinstance(probe.get("rows"), list):
        result = {
            "identity": "tesouro",
            "status": "source_unavailable" if probe.get("failure_class") == "transport" else "invalid_source",
            "error_code": probe.get("error_code") or "invalid_probe_payload",
            "source_checked_at": checked_at,
        }
        for key in ("http_status", "retry_after_seconds", "elapsed_seconds"):
            if probe.get(key) is not None:
                result[key] = probe[key]
        return [result]
    saved: list[dict[str, Any]] = []
    for row in database.get("treasury", []):
        data = _source_object(row.get("data_json"))
        if data is not None:
            saved.append({**data, "_db_base_date": row.get("base_date"), "_db_fetched_at": row.get("fetched_at")})
    results: list[dict[str, Any]] = []
    db_names = {"buy_rate": "buyRate", "sell_rate": "sellRate", "buy_price": "buyPrice", "sell_price": "sellPrice", "base_price": "basePrice"}
    raw_rows = probe.get("rows")
    rows: list[SourceRow] = [
        cast(SourceRow, row) for row in cast(list[object], raw_rows) if isinstance(row, dict)
    ] if isinstance(raw_rows, list) else []
    for source in rows:
        kind, maturity = str(source.get("kind") or ""), str(source.get("maturity") or "")
        normalized = (_date(maturity) or date.min).isoformat()
        dbrow = next((row for row in saved if row.get("name") == kind and (_date(str(row.get("maturityDate", ""))) or date.min).isoformat() == normalized), None)
        ref = _date(source.get("base_date"))
        for source_field, database_field in db_names.items():
            results.append(compare_observation(
                ref.isoformat() if ref else None,
                source.get(source_field),
                dbrow.get("_db_base_date") if dbrow else None,
                dbrow.get(database_field) if dbrow else None,
                source_checked_at=checked_at,
                db_fetched_at=dbrow.get("_db_fetched_at") if dbrow else None,
                identity=f"tesouro:{kind}:{maturity}:{source_field}",
            ))
    return results or [{"identity": "tesouro", "status": "invalid_source", "error_code": "no_valid_bond_rows", "source_checked_at": checked_at}]


def _row_values(probe: SourceRow, provider: str) -> dict[str, tuple[object, ...]]:
    rows = probe.get("rows")
    if not isinstance(rows, list):
        return {}
    values: dict[str, tuple[object, ...]] = {}
    for item in cast(list[object], rows):
        if not isinstance(item, dict):
            continue
        raw = cast(SourceRow, item)
        if provider == "b3":
            identity = str(raw.get("symbol") or "")
            values[identity] = (raw.get("trade_date"), raw.get("security_id"), _number(raw.get("rate")))
        else:
            identity = f"{raw.get('kind')}|{raw.get('maturity')}"
            values[identity] = (
                raw.get("base_date"),
                *(_number(raw.get(key)) for key in ("buy_rate", "sell_rate", "buy_price", "sell_price", "base_price")),
            )
    return values


def _treasury_identity(kind: object, maturity: object, bond_code: object | None = None) -> str | None:
    parsed_maturity = maturity if isinstance(maturity, date) else _date(maturity)
    normalized_kind = str(kind or "").strip()
    if not normalized_kind or parsed_maturity is None:
        return None
    maturity_text = parsed_maturity.strftime("%d/%m/%Y")
    code = (
        str(bond_code)
        if bond_code is not None
        else str(zlib.crc32(f"{normalized_kind}|{maturity_text}".encode()) & 0x7FFFFFFF)
    )
    return f"{code}|{normalized_kind}|{parsed_maturity.isoformat()}"


def _treasury_expected_active(
    database: SourceDatabase,
    cutoff: date,
) -> tuple[date | None, set[str], list[str]]:
    """Choose the latest valid quote vintage first, then exclude its expired titles."""
    eligible: list[tuple[date, str, date]] = []
    metadata_errors: list[str] = []
    for row in database.get("treasury", []):
        base = _date(row.get("base_date"))
        data = _source_object(row.get("data_json"))
        if base is None or base > cutoff:
            continue
        if data is None:
            metadata_errors.append("treasury_quote_payload_invalid")
            continue
        maturity = _date(data.get("maturityDate"))
        identity = _treasury_identity(data.get("name"), maturity, row.get("bond_code"))
        if maturity is None or identity is None or row.get("bond_code") is None:
            metadata_errors.append("treasury_quote_identity_invalid")
            continue
        eligible.append((base, identity, maturity))
    if not eligible:
        metadata_errors.append("no_valid_treasury_quote_vintage_at_or_before_cutoff")
        return None, set(), sorted(set(metadata_errors))
    latest_base = max(base for base, _identity, _maturity in eligible)
    expected = {
        identity
        for base, identity, maturity in eligible
        if base == latest_base and maturity >= latest_base
    }
    return latest_base, expected, sorted(set(metadata_errors))


def _treasury_missing_active(database: SourceDatabase, probe: SourceRow, cutoff: date) -> list[str]:
    _db_reference, expected, _metadata_errors = _treasury_expected_active(database, cutoff)
    source_reference = _date(probe.get("source_reference"))
    source_rows = probe.get("rows")
    if not expected or source_reference is None or not isinstance(source_rows, list):
        return []
    observed: set[str] = set()
    for item in cast(list[object], source_rows):
        if not isinstance(item, dict):
            continue
        row = cast(SourceRow, item)
        if _date(row.get("base_date")) != source_reference:
            continue
        if (_date(row.get("maturity")) or date.min) < source_reference:
            continue
        identity = _treasury_identity(row.get("kind"), row.get("maturity"))
        if identity is not None:
            observed.add(identity)
    return sorted(expected - observed)


def _probe_summary(probe: dict[str, Any]) -> dict[str, Any]:
    summary = {key: probe.get(key) for key in (
        "status", "failure_class", "error_code", "http_status", "retry_after_seconds",
        "cutoff_at", "source_checked_at", "source_reference", "row_count", "payload_bytes", "payload_sha256", "elapsed_seconds",
    ) if probe.get(key) is not None}
    missing = probe.get("coverage_missing_identities")
    if isinstance(missing, list):
        summary["coverage_missing_identities"] = missing
    for key in ("database_coverage_status", "database_quote_reference", "database_metadata_errors"):
        if probe.get(key) is not None:
            summary[key] = probe[key]
    return summary


def _validate_environment_probe(
    provider: str,
    environment: str,
    probe: SourceRow,
    cutoff_at: str,
    database: SourceDatabase,
) -> dict[str, Any]:
    """Fail closed on cross-provider, cross-environment, or cutoff-mismatched responses."""
    if probe.get("provider") != provider or probe.get("environment") != environment:
        return {**probe, "status": "invalid_source", "failure_class": "source_contract", "error_code": "probe_identity_mismatch"}
    if probe.get("status") != "valid":
        return probe
    if probe.get("cutoff_at") != cutoff_at:
        return {**probe, "status": "invalid_source", "failure_class": "source_contract", "error_code": "probe_cutoff_mismatch"}
    reference = _date(probe.get("source_reference"))
    cutoff = _probe_cutoff(cutoff_at)
    raw_rows = probe.get("rows")
    if reference is None or reference > cutoff or not isinstance(raw_rows, list) or not raw_rows:
        return {**probe, "status": "invalid_source", "failure_class": "source_contract", "error_code": "invalid_probe_coverage"}
    identities: set[str] = set()
    for item in cast(list[object], raw_rows):
        if not isinstance(item, dict):
            return {**probe, "status": "invalid_source", "failure_class": "source_contract", "error_code": "invalid_probe_row"}
        row = cast(SourceRow, item)
        if provider == "b3":
            identity = str(row.get("symbol") or "")
            valid = bool(identity and row.get("trade_date") == reference.isoformat() and row.get("security_id") and _number(row.get("rate")) is not None)
        else:
            identity = f"{row.get('kind')}|{row.get('maturity')}"
            valid = bool(row.get("kind") and _date(row.get("maturity")) and _date(row.get("base_date")) == reference)
            valid = valid and all(_number(row.get(key)) is not None for key in ("buy_rate", "sell_rate", "buy_price", "sell_price", "base_price"))
        if not valid or identity in identities:
            return {**probe, "status": "invalid_source", "failure_class": "source_contract", "error_code": "invalid_or_duplicate_probe_identity"}
        identities.add(identity)
    if provider == "tesouro":
        quote_reference, expected_active, metadata_errors = _treasury_expected_active(database, cutoff)
        if metadata_errors:
            return {
                **probe,
                "status": "invalid_source",
                "failure_class": "source_contract",
                "error_code": "database_active_universe_incomplete",
                "database_coverage_status": "baseline_unavailable",
                "database_metadata_errors": metadata_errors,
            }
        probe = {
            **probe,
            "database_quote_reference": quote_reference.isoformat() if quote_reference else None,
            "database_coverage_status": "active_set_empty" if not expected_active else "active_set_known",
        }
        missing_active = _treasury_missing_active(database, probe, cutoff)
        if missing_active:
            return {
                **probe,
                "status": "invalid_source",
                "failure_class": "source_contract",
                "error_code": "missing_active_identity",
                "coverage_missing_identities": missing_active,
            }
    return probe


def _dual_probe_results(
    provider: str,
    database: SourceDatabase,
    checked_at: str,
    environments: dict[str, dict[str, Any]],
) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    expected_providers = {"b3": "b3", "tesouro": "tesouro"}
    for environment in ("github_actions", "application"):
        if environment not in environments:
            environments[environment] = {
                "provider": provider, "environment": environment, "status": "source_unavailable",
                "failure_class": "transport", "error_code": "environment_probe_missing",
            }
    normalized: dict[str, SourceRow] = {
        environment: cast(
            SourceRow,
            _validate_environment_probe(provider, environment, cast(SourceRow, probe), checked_at, database),
        )
        for environment, probe in environments.items()
    }
    for environment, probe in tuple(normalized.items()):
        if probe.get("provider") != expected_providers[provider]:
            normalized[environment] = {
                **probe, "status": "invalid_source", "failure_class": "source_contract",
                "error_code": "probe_provider_mismatch",
            }
    valid: dict[str, SourceRow] = {name: probe for name, probe in normalized.items() if probe.get("status") == "valid"}
    summaries: dict[str, dict[str, Any]] = {name: _probe_summary(probe) for name, probe in normalized.items()}
    if len(valid) == 2:
        observation_sets = {name: _row_values(probe, provider) for name, probe in valid.items()}
        first_observation_set = next(iter(observation_sets.values()))
        if any(values != first_observation_set for values in observation_sets.values()):
            identities: set[str] = set()
            for values in observation_sets.values():
                identities.update(values.keys())
            results = [{
                "identity": f"{provider}:{identity}", "status": "source_disagreement",
                "error_code": "environment_observations_differ", "source_checked_at": checked_at,
                "environment_comparisons": {
                    name: {"reference": valid[name].get("source_reference"), "observation": list(observation_sets[name].get(identity, ())) }
                    for name in valid
                },
            } for identity in sorted(identities)]
            return results, {"status": "source_disagreement", "environments": summaries}
        chosen_name = next(iter(valid))
        compare = _compare_b3_probe if provider == "b3" else _compare_treasury_probe
        results = compare(database, str(valid[chosen_name].get("source_checked_at") or checked_at), valid[chosen_name])
        for result in results:
            result["corroborated"] = True
            result["environment_probes"] = summaries
        return results, {"status": "corroborated", "environments": summaries}
    if len(valid) == 1:
        name, probe = next(iter(valid.items()))
        other_name = next(environment for environment in normalized if environment != name)
        other = normalized[other_name]
        if other.get("failure_class") != "transport":
            source_checked_at = str(probe.get("source_checked_at") or checked_at)
            results = _compare_b3_probe(database, source_checked_at, probe) if provider == "b3" else _compare_treasury_probe(database, source_checked_at, probe)
            for result in results:
                result["status"] = "source_contract_disagreement"
                result["error_code"] = "environment_source_contract_failure"
                result["environment_probes"] = summaries
            return results, {"status": "source_contract_disagreement", "environments": summaries}
        compare = _compare_b3_probe if provider == "b3" else _compare_treasury_probe
        results = compare(database, str(probe.get("source_checked_at") or checked_at), probe)
        for result in results:
            result["verified_by"] = name
            result["transport_warning"] = other_name
            result["environment_probes"] = summaries
        return results, {"status": "verified_with_transport_warning", "environments": summaries}
    has_contract_failure = any(probe.get("failure_class") == "source_contract" for probe in normalized.values())
    terminal_status = "source_contract_disagreement" if has_contract_failure else "source_unavailable"
    error_code = "environment_source_contract_failure" if has_contract_failure else "no_environment_returned_valid_source"
    results = [{
        "identity": provider, "status": terminal_status, "error_code": error_code,
        "source_checked_at": checked_at, "environment_probes": summaries,
    }]
    return results, {"status": terminal_status, "environments": summaries}


def run_audit(
    snapshot: dict[str, Any],
    *,
    clock: datetime | None = None,
    provider_environment_probes: dict[str, dict[str, dict[str, Any]]] | None = None,
) -> dict[str, Any]:
    checked_at = (clock or datetime.now(timezone.utc)).isoformat()
    database = _database_snapshot(snapshot.get("database"))
    database_read_at = snapshot.get("database_snapshot_at")
    database_read_at = database_read_at if isinstance(database_read_at, str) else None
    focus_as_of = database_read_at or checked_at
    production_host_verified = _production_snapshot_host_verified(snapshot.get("request_provenance"))
    captured_value = snapshot.get("captured_at")
    captured_at = captured_value if isinstance(captured_value, str) else checked_at
    source_cutoff_day = _as_of_utc(captured_at, clock or datetime.now(timezone.utc)).date()
    groups: dict[str, list[dict[str, Any]]] = {
        "sgs": _probe_sgs(database, checked_at, source_cutoff_day),
        "focus": _probe_focus(database, checked_at, focus_as_of),
        "sidra": _probe_sidra(database, checked_at),
        "b3_di": _probe_b3(database, checked_at) if provider_environment_probes is None else [],
        "anbima": _probe_anbima(database, checked_at),
        "treasury": _probe_treasury(database, checked_at) if provider_environment_probes is None else [],
    }
    dual_probe_summaries: dict[str, Any] = {}
    if provider_environment_probes is not None:
        for provider, source_key in (("b3", "b3_di"), ("tesouro", "treasury")):
            comparisons, summary = _dual_probe_results(
                provider,
                database,
                checked_at,
                provider_environment_probes.get(provider, {}),
            )
            groups[source_key] = comparisons
            dual_probe_summaries[provider] = summary
    rows = [row for records in groups.values() for row in records]
    failures = {"source_newer", "source_unavailable", "source_anomaly_inconclusive", "invalid_source", "unsupported_coverage", "database_missing", "same_reference_value_mismatch", "database_ahead", "source_disagreement", "source_contract_disagreement"}
    problems = sum(row.get("status") in failures for row in rows)
    expected_focus = {f"focus:{key}" for key in (*FOCUS_ANNUAL, FOCUS_12M)}
    focus_roots = {tuple(str(row.get("identity", "")).split(":", 2)[:2]) for row in groups["focus"]}
    focus_identity_coverage = _focus_coverage(groups["focus"], focus_as_of, database)
    expected_sidra = {f"ibge_sidra:{spec.key}" for spec in SIDRA_DATASETS.values()}
    audit_limits = database.get("audit_limits", [])
    extraction_rows = database.get("source_extractions", [])
    extraction_summary: dict[str, dict[str, Any]] = {}
    for extraction in extraction_rows:
        source = str(extraction.get("source", ""))
        indicator = str(extraction.get("indicator", ""))
        key = f"{source}:{indicator}"
        item = extraction_summary.setdefault(key, {"latest_attempt": None, "latest_network_extraction": None})
        if item["latest_attempt"] is None:
            item["latest_attempt"] = {name: extraction.get(name) for name in (
                "status", "started_at", "finished_at", "source_data_date", "source_reference_min",
                "source_reference_max", "received_count", "valid_count", "persisted_count",
                "run_id", "task_key", "cycle_id", "error_code", "network_called",
            )}
        if extraction.get("network_called") and item["latest_network_extraction"] is None:
            item["latest_network_extraction"] = {name: extraction.get(name) for name in (
                "status", "extracted_at", "source_data_date", "source_reference_min",
                "source_reference_max", "valid_count", "persisted_count", "run_id", "task_key",
            )}
    coverage_complete = (
        production_host_verified
        and
        {int(row["identity"].split(":", 1)[1]) for row in groups["sgs"] if str(row.get("identity", "")).startswith("bcb_sgs:")} == set(SGS_CODES)
        and {f"{root[0]}:{root[1]}" for root in focus_roots} == expected_focus
        and focus_identity_coverage["complete"]
        and {str(row.get("identity")) for row in groups["sidra"]} == expected_sidra
        and bool(groups["b3_di"]) and not any(row.get("identity") == "b3:di" for row in groups["b3_di"])
        and bool(groups["anbima"]) and not any(str(row.get("status")) in {"source_unavailable", "invalid_source"} for row in groups["anbima"])
        and bool(groups["treasury"]) and not any(str(row.get("status")) in {"source_unavailable", "invalid_source"} for row in groups["treasury"])
        and not any(_count_value(limit.get(key)) > 0 for limit in audit_limits
                    for key in ("focus_current_identity_omitted", "focus_vintage_identity_omitted", "anbima_latest_series_omitted"))
    )
    limit_row: SourceRow = audit_limits[0] if audit_limits else {}
    storage_divergences = sum(bool(row.get("database_storage_divergence")) for row in groups["focus"])
    future_database_count = _count_value(limit_row.get("focus_future_rows_excluded")) + _count_value(
        limit_row.get("focus_vintage_future_rows_excluded"),
    )
    if storage_divergences:
        problems += storage_divergences
    if future_database_count:
        problems += 1
    return {
        "schema_version": "source-audit-report-v2",
        "generated_at": checked_at,
        "database_snapshot_at": snapshot.get("captured_at"),
        "database_read_at": database_read_at,
        "provenance": {
            "runtime": snapshot.get("runtime", {"commit_sha": None, "environment": None}),
            "request": snapshot.get("request_provenance", {"matches_expected_production_host": False}),
            "focus_database": snapshot.get("database_provenance", {}),
            "focus_storage_divergence_count": storage_divergences,
            "focus_coverage_as_of": focus_as_of,
            "future_database_observations": {
                "count": future_database_count,
                "current_latest_date": limit_row.get("focus_latest_future_date"),
                "vintage_latest_date": limit_row.get("focus_vintage_latest_future_date"),
            },
            "snapshot_capture_gap_ms": _snapshot_gap_ms(database_read_at, snapshot.get("captured_at")),
            "source_extractions": extraction_summary,
            "source_extractions_truncated": len(extraction_rows) >= 300,
            "provider_environment_probes": dual_probe_summaries,
        },
        "catalog_version": CATALOG_VERSION,
        "status": "healthy" if coverage_complete and problems == 0 else "degraded",
        "coverage": {"expected_static": 75, "checked": len(rows), "complete": coverage_complete,
                      "counts_by_source": {name: len(results) for name, results in groups.items()},
                      "database_limit_exceeded": any(_count_value(limit_row.get(key)) > 0 for key in (
                          "focus_current_identity_omitted", "focus_vintage_identity_omitted", "anbima_latest_series_omitted",
                      )),
                       "historical_rows_omitted": {
                           key: _count_value(limit_row.get(key)) for key in (
                               "focus_history_rows_omitted", "focus_vintage_history_rows_omitted", "anbima_history_rows_omitted",
                           )
                       },
                       "focus_identity_coverage": focus_identity_coverage,
                       "snapshot_host_verified": production_host_verified},
        "counts_by_status": {status: sum(row.get("status") == status for row in rows) for status in sorted({str(row.get("status")) for row in rows})},
        "sources": groups,
    }


def _snapshot_gap_ms(database_read_at: str | None, captured_at: object) -> int | None:
    if database_read_at is None or not isinstance(captured_at, str):
        return None
    try:
        database_time = datetime.fromisoformat(database_read_at.replace("Z", "+00:00"))
        captured_time = datetime.fromisoformat(captured_at.replace("Z", "+00:00"))
    except ValueError:
        return None
    if database_time.tzinfo is None or captured_time.tzinfo is None:
        return None
    return max(0, int((captured_time - database_time).total_seconds() * 1000))


def render_markdown(report: dict[str, Any]) -> str:
    lines = ["# X-BRAY — auditoria semanal de fontes", "", f"- Resultado: **{report['status']}**",
             f"- Verificado em: `{report['generated_at']}`", f"- Snapshot do banco: `{report.get('database_snapshot_at')}`",
             f"- Cobertura: {report['coverage']['checked']} itens; completa: {report['coverage']['complete']}", "",
             f"- Proveniência Focus: `{json.dumps(report.get('provenance', {}), ensure_ascii=False, sort_keys=True)}`", "",
             "| Identidade | Status | Referência da fonte | Valor da fonte | Referência do banco | Valor do banco | Diagnóstico |", "|---|---|---:|---:|---:|---:|---|"]
    for group in report["sources"].values():
        for row in group:
            diagnostic = ", ".join(
                f"{key}={row[key]}" for key in (
                    "error_code", "warning_code", "source_future_observation_count", "source_latest_future_date",
                    "retry_after_seconds", "comparison_basis", "database_vintage_reference",
                    "verified_by", "transport_warning", "corroborated", "environment_comparisons",
                ) if row.get(key) is not None
            )
            lines.append("| {identity} | {status} | {source_reference} | {source_value} | {database_reference} | {database_value} | {diagnostic} |".format(
                identity=row.get("identity", "unknown"), status=row.get("status", "unknown"),
                source_reference=row.get("source_reference", "—"), source_value=row.get("source_value", "—"),
                database_reference=row.get("database_reference", "—"), database_value=row.get("database_value", "—"), diagnostic=diagnostic,
            ))
    return "\n".join(lines) + "\n"
