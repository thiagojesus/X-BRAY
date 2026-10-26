"""Independent parsers for bounded candidate-origin audit evidence.

Economic values are hashed using the persisted float/JSON representation, not
display rounding or Decimal text normalization.
"""
from __future__ import annotations

import csv
import hashlib
import io
import json
import re
import unicodedata
from datetime import date
from decimal import Decimal, InvalidOperation
from typing import Any, NoReturn, cast

from indicator_catalog import INDICATOR_CATALOG


class CandidateOriginError(ValueError):
    """Untrusted source data fails the caller-supplied contract."""


def _fail(code: str) -> NoReturn:
    raise CandidateOriginError(code)


def _value(raw: object) -> tuple[str, str]:
    if not isinstance(raw, str) or not raw.strip():
        _fail("missing_value")
    text = raw.strip()
    if not re.fullmatch(r"-?[0-9]+(?:[,.][0-9]+)?", text, flags=re.ASCII):
        _fail("malformed_value")
    try:
        value = Decimal(text.replace(",", "."))
    except InvalidOperation:
        _fail("malformed_value")
    if not value.is_finite():
        _fail("nonfinite_value")
    canonical = format(value.normalize(), "f")
    if canonical in {"-0", ""}:
        canonical = "0"
    persisted = json.dumps(float(value), sort_keys=True, separators=(",", ":"), allow_nan=False)
    return canonical, hashlib.sha256(persisted.encode("ascii")).hexdigest()


def _day(raw: object, fmt: str) -> date:
    if not isinstance(raw, str) or not re.fullmatch(r"[0-9]{2}/[0-9]{2}/[0-9]{4}", raw, flags=re.ASCII):
        _fail("invalid_date")
    try:
        return date(int(raw[6:]), int(raw[3:5]), int(raw[:2]))
    except ValueError:
        _fail("invalid_date")


def parse_sgs_csv(payload: bytes, *, contract_id: str, expected_unit: str,
                  required_identities: set[tuple[str, ...]], as_of: date) -> list[dict[str, Any]]:
    """Parse point-series CSV or full-period TR226 CSV against explicit scope."""
    known = {f"sgs:{spec.provider_code}" for spec in INDICATOR_CATALOG.values()}
    if contract_id not in known:
        _fail("unknown_contract")
    period = contract_id == "sgs:226"
    try:
        text = payload.decode("utf-8-sig", errors="strict")
        reader = csv.DictReader(io.StringIO(text, newline=""), delimiter=";" if ";" in text.partition("\n")[0] else ",")
    except (UnicodeDecodeError, csv.Error):
        _fail("invalid_csv")
    headers = {name.strip().lower() for name in reader.fieldnames or []}
    required_headers = {"data", "valor"}
    if reader.fieldnames is None or not required_headers.issubset(headers) or headers - required_headers - {"data fim", "unidade"}:
        _fail("unexpected_headers")
    if period and "data fim" not in headers:
        _fail("missing_period_end")
    if not period and "data fim" in headers:
        _fail("unexpected_period_end")
    rows: dict[tuple[str, ...], dict[str, Any]] = {}
    for raw in reader:
        row = {str(key).strip().lower(): value.strip() if isinstance(value, str) else "" for key, value in raw.items() if key is not None}
        if set(row) != headers or ("unidade" in headers and row.get("unidade") != expected_unit):
            _fail("unit_or_row_mismatch")
        start = _day(row.get("data"), "strict")
        end = _day(row.get("data fim"), "strict") if period else start
        if period and end < start:
            _fail("invalid_period")
        identity = ("226", start.isoformat(), end.isoformat()) if period else (contract_id.split(":", 1)[1], start.isoformat())
        if identity not in required_identities:
            _fail("unexpected_identity")
        number, digest = _value(row.get("valor"))
        item = {"identity": identity, "reference_start": start.isoformat(), "reference_end": end.isoformat(),
                "value": number, "unit": expected_unit, "value_hash": digest}
        if identity in rows and rows[identity] != item:
            _fail("conflicting_duplicate")
        rows[identity] = item
    if set(rows) != required_identities:
        _fail("incomplete_scope")
    # A period ending in the future is valid when its start is eligible.
    if any(date.fromisoformat(item["reference_start"]) > as_of for item in rows.values()):
        _fail("future_reference")
    return [rows[key] for key in sorted(rows)]


def parse_sgs_json(payload: bytes, *, contract_id: str, required_identities: set[tuple[str, ...]],
                    as_of: date, expected_unit: str) -> list[dict[str, Any]]:
    """Independently parse the primary SGS JSON representation."""
    known = {f"sgs:{spec.provider_code}" for spec in INDICATOR_CATALOG.values()}
    if contract_id not in known:
        _fail("unknown_contract")
    try:
        decoded = json.loads(payload)
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise CandidateOriginError("invalid_json") from exc
    if not isinstance(decoded, list) or not decoded:
        _fail("invalid_shape")
    raw_rows = cast(list[object], decoded)
    if any(not isinstance(row, dict) for row in raw_rows):
        _fail("invalid_shape")
    records = cast(list[dict[str, Any]], raw_rows)
    is_period = contract_id == "sgs:226"
    result: dict[tuple[str, ...], dict[str, Any]] = {}
    for row in records:
        start = _day(row.get("data"), "strict")
        end = _day(row.get("dataFim"), "strict") if is_period else start
        if is_period and end < start:
            _fail("invalid_period")
        identity = ("226", start.isoformat(), end.isoformat()) if is_period else (
            contract_id.split(":", 1)[1], start.isoformat()
        )
        if identity not in required_identities:
            _fail("unexpected_identity")
        if start > as_of:
            _fail("future_reference")
        number, digest = _value(row.get("valor"))
        item = {
            "identity": identity, "reference_start": start.isoformat(),
            "reference_end": end.isoformat(), "value": number,
            "unit": expected_unit, "value_hash": digest,
        }
        if identity in result and result[identity] != item:
            _fail("conflicting_duplicate")
        result[identity] = item
    if set(result) != required_identities:
        _fail("incomplete_scope")
    return [result[key] for key in sorted(result)]


def parse_sidra_v3(payload: bytes | str | dict[str, Any], *, required_identities: set[tuple[str, ...]],
                   as_of: date, expected_unit: str, expected_base_version: str,
                   table_id: str = "8888", variable_id: str = "12607",
                   classification_id: str | None = "544", category_id: str | None = "129314",
                   category_label: str = "Indústria geral", territory_level: str = "N1",
                   territory_code: str = "1", identity_unit: str | None = None) -> list[dict[str, Any]]:
    """Parse SIDRA Aggregados v3 JSON; metadata is checked, never inferred."""
    if expected_unit not in {"Percentual", "Número-índice", "Mil Reais", "Reais"} or not expected_base_version:
        _fail("unknown_contract")
    try:
        obj = json.loads(payload) if isinstance(payload, (bytes, str)) else payload
    except (json.JSONDecodeError, UnicodeDecodeError):
        _fail("invalid_json")
    if not isinstance(obj, list) or not obj:
        _fail("invalid_shape")
    raw_rows = cast(list[object], obj)
    if not all(isinstance(row, dict) for row in raw_rows):
        _fail("invalid_shape")
    obj = cast(list[dict[str, Any]], raw_rows)
    result: dict[tuple[str, ...], dict[str, Any]] = {}
    for row in obj:
        if "D1C" in row or "D2C" in row or "N1C" in row:
            _fail("flattened_primary_not_v3")
        if str(row.get("id")) != variable_id:
            _fail("wrong_variable")
        if row.get("unidade") != expected_unit:
            _fail("unit_or_base_mismatch")
        raw_outcomes = row.get("resultados")
        if not isinstance(raw_outcomes, list) or not raw_outcomes:
            _fail("invalid_nested_shape")
        if any(not isinstance(outcome, dict) for outcome in cast(list[object], raw_outcomes)):
            _fail("invalid_nested_shape")
        outcomes = cast(list[dict[str, Any]], raw_outcomes)
        for outcome in outcomes:
            raw_classifications = outcome.get("classificacoes", [])
            if not isinstance(raw_classifications, list):
                _fail("invalid_nested_shape")
            classifications = cast(list[object], raw_classifications)
            if classification_id is None:
                category_ok = classifications == []
            else:
                category_ok = _sidra_category_matches(
                    classifications, classification_id, category_id, category_label
                )
            if not category_ok:
                _fail("wrong_territory_or_category")
            raw_series_rows = outcome.get("series")
            if not isinstance(raw_series_rows, list) or not raw_series_rows:
                _fail("invalid_nested_shape")
            series_rows = cast(list[dict[str, Any]], raw_series_rows)
            if any(not isinstance(series, dict) for series in cast(list[object], raw_series_rows)):
                _fail("invalid_nested_shape")
            for series in series_rows:
                locality = series.get("localidade")
                values = series.get("serie")
                if not isinstance(locality, dict) or not isinstance(values, dict):
                    _fail("invalid_nested_shape")
                locality_obj = cast(dict[str, Any], locality)
                values_obj = cast(dict[str, Any], values)
                level = locality_obj.get("nivel")
                if not isinstance(level, dict):
                    _fail("invalid_nested_shape")
                level_obj = cast(dict[str, Any], level)
                if str(locality_obj.get("id")) != territory_code or str(level_obj.get("id")) != territory_level:
                    _fail("wrong_territory_or_category")
                for reference, raw in values_obj.items():
                    if not re.fullmatch(r"[0-9]{4}(?:[0-9]{2})?", reference, re.ASCII):
                        _fail("invalid_reference")
                    try:
                        day = date(int(reference[:4]), int(reference[4:]) if len(reference) == 6 else 1, 1)
                    except ValueError:
                        _fail("invalid_reference")
                    if day > as_of:
                        _fail("future_reference")
                    identity = (variable_id, table_id, territory_level, territory_code,
                                classification_id or "", category_id or "", reference,
                                identity_unit or expected_unit, expected_base_version)
                    if identity not in required_identities:
                        _fail("unexpected_identity")
                    if raw in {"...", "X", ".."}:
                        _fail("unavailable_value")
                    number, digest = ("0", hashlib.sha256(b"0.0").hexdigest()) if raw == "-" else _value(str(raw) if raw is not None else "")
                    item = {"identity": identity, "reference": reference, "value": number, "unit": expected_unit, "value_hash": digest}
                    if identity in result and result[identity] != item:
                        _fail("conflicting_duplicate")
                    result[identity] = item
    if set(result) != required_identities:
        _fail("incomplete_scope")
    return [result[k] for k in sorted(result)]


def _sidra_category_matches(classifications: list[object], classification_id: str,
                            category_id: str | None, label: str) -> bool:
    for item in classifications:
        if not isinstance(item, dict):
            continue
        classification = cast(dict[str, Any], item)
        raw_category_map = classification.get("categoria")
        if not isinstance(raw_category_map, dict):
            continue
        category_map = cast(dict[str, Any], raw_category_map)
        if (str(classification.get("id")) == classification_id and category_id is not None and category_id in category_map
                and _label(category_map[category_id]) == _label(label)):
            return True
    return False


def parse_sidra_primary(payload: bytes, *, required_identities: set[tuple[str, ...]],
                        as_of: date, variable_id: str, unit_code: str,
                        territory_code: str, category_id: str | None,
                        identity_unit: str, base_version: str) -> list[dict[str, Any]]:
    """Parse SIDRA's independent official values representation (v1 rows)."""
    try:
        obj = json.loads(payload)
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise CandidateOriginError("invalid_primary_json") from exc
    if not isinstance(obj, list) or not obj or any(not isinstance(row, dict) for row in cast(list[object], obj)):
        _fail("invalid_primary_shape")
    obj = cast(list[dict[str, Any]], obj)
    result: dict[tuple[str, ...], dict[str, Any]] = {}
    for row in obj:
        if str(row.get("D1C", "")) != territory_code or str(row.get("D2C", "")) != variable_id:
            continue
        if str(row.get("MC", "")) != unit_code:
            _fail("unit_mismatch")
        if category_id is not None and str(row.get("D4C", "")) != category_id:
            continue
        reference = row.get("D3C")
        if not isinstance(reference, str) or re.fullmatch(r"[0-9]{4}(?:[0-9]{2})?", reference, re.ASCII) is None:
            _fail("invalid_reference")
        try:
            date(int(reference[:4]), int(reference[4:]) if len(reference) == 6 else 1, 1)
        except ValueError:
            _fail("invalid_reference")
        if date(int(reference[:4]), int(reference[4:]) if len(reference) == 6 else 1, 1) > as_of:
            _fail("future_reference")
        # Classification identifiers are supplied by the trusted scoped row
        # in D2C on the legacy values route; caller identity contract must bind it.
        matching = next((key for key in required_identities if key[6] == reference), None)
        if matching is None:
            _fail("unexpected_identity")
        raw = row.get("V")
        if raw == "-":
            number, digest = "0", hashlib.sha256(b"0.0").hexdigest()
        else:
            number, digest = _value(raw)
        item = {"identity": matching, "reference": reference, "value": number,
                "unit": identity_unit, "value_hash": digest}
        if matching in result and result[matching] != item:
            _fail("conflicting_duplicate")
        result[matching] = item
    if set(result) != required_identities:
        _fail("incomplete_scope")
    return [result[key] for key in sorted(result)]


def _label(value: object) -> str:
    if not isinstance(value, str):
        return ""
    normalized = unicodedata.normalize("NFC", value)
    return " ".join(normalized.split()).casefold()
