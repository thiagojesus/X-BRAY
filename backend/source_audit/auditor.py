"""Independent bounded source probes and semantic comparisons; no ingestion calls."""

from __future__ import annotations

import csv
import io
import json
import math
import time
import xml.etree.ElementTree as ET
import zipfile
from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal, InvalidOperation
from typing import Any, TypeAlias, cast

import httpx

from provider_http import get_provider_response_bounded
from source_audit.catalog import (
    ANBIMA_URL,
    B3_URL,
    FOCUS_12M,
    FOCUS_ANNUAL,
    SGS_CODES,
    SIDRA_DATASETS,
    TREASURY_URL,
)

MAX_JSON_BYTES = 5 * 1024 * 1024
MAX_BINARY_BYTES = 30 * 1024 * 1024
MAX_CSV_BYTES = 64 * 1024 * 1024
PER_SOURCE_DEADLINE = 45
FOCUS_TOP = 200
_B3_NS = "urn:bvmf.217.01.xsd"
SourceRow: TypeAlias = dict[str, object]
SourceDatabase: TypeAlias = dict[str, list[SourceRow]]


def _source_object(value: object) -> SourceRow | None:
    if not isinstance(value, dict):
        return None
    result: SourceRow = {}
    for key, item in cast(dict[object, object], value).items():
        if not isinstance(key, str):
            return None
        result[key] = item
    return result


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


def _get(url: str, params: dict[str, str] | None, cap: int, deadline: int = PER_SOURCE_DEADLINE) -> bytes:
    return get_provider_response_bounded(
        url, params=params, max_bytes=cap, deadline_seconds=deadline,
        headers={"Accept": "application/json, text/csv, application/vnd.ms-excel, application/zip, */*"},
    ).content


def _json(url: str, params: dict[str, str] | None = None) -> object:
    body = _get(url, params, MAX_JSON_BYTES)
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


def _number(value: Any) -> Decimal | None:
    if isinstance(value, bool) or value is None:
        return None
    try:
        result = Decimal(str(value).replace(",", "."))
    except (InvalidOperation, ValueError):
        return None
    return result if result.is_finite() else None


def _string_field(row: SourceRow, name: str) -> str | None:
    value = row.get(name)
    return value if isinstance(value, str) else None


def _valid_latest(records: list[dict[str, Any]], date_key: str, value_key: str) -> dict[str, Any] | None:
    valid = [record for record in records if _date(record.get(date_key)) is not None and _number(record.get(value_key)) is not None]
    return max(valid, key=lambda row: _date(row[date_key]) or date.min, default=None)


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
    """Compare exact references and destination-float precision without UI rounding."""
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
    db_float = float(db_number)
    src_float = float(source_number)
    precision = max(math.ulp(db_float), math.ulp(src_float))
    base["status"] = "matched" if abs(db_float - src_float) <= precision else "same_reference_value_mismatch"
    return base


def _db_sgs(rows: list[dict[str, Any]]) -> dict[int, dict[str, Any]]:
    return {int(row["series_code"]): row for row in rows if row.get("series_code") is not None}


def _probe_sgs(database: SourceDatabase, checked_at: str) -> list[dict[str, Any]]:
    db_rows = _db_sgs(database.get("sgs", []))
    results: list[dict[str, Any]] = []
    for code in SGS_CODES:
        try:
            data = _json(f"https://api.bcb.gov.br/dados/serie/bcdata.sgs.{code}/dados/ultimos/5", {"formato": "json"})
            if not isinstance(data, list):
                raise TypeError("invalid_source_shape")
            source_payload = cast(list[object], data)
            source_records = _source_rows(source_payload)
            if len(source_records) != len(source_payload):
                raise TypeError("invalid_source_row")
            latest = _valid_latest(source_records, "data", "valor")
            db = db_rows.get(code, {})
            result = compare_observation(
                latest.get("data") if latest else None,
                latest.get("valor") if latest else None,
                db.get("date"), db.get("value"), source_checked_at=checked_at,
                db_fetched_at=db.get("fetched_at"), identity=f"bcb_sgs:{code}",
            )
            result["source_url"] = f"https://api.bcb.gov.br/dados/serie/bcdata.sgs.{code}/dados/ultimos/5?formato=json"
            if latest is None and data:
                result["status"] = "invalid_source"
                result["error_code"] = "no_valid_source_observation"
            results.append(result)
        except Exception as exc:  # noqa: BLE001 - isolate failures per independently probed series.
            results.append({"identity": f"bcb_sgs:{code}", "status": "source_unavailable", "error_code": type(exc).__name__, "source_checked_at": checked_at})
    return results


def _focus_key(record: dict[str, Any]) -> tuple[str, str, str, str]:
    return (
        str(record.get("Indicador", "")), str(record.get("DataReferencia", "")),
        str(record.get("baseCalculo", "")), str(record.get("Suavizada", "")),
    )


def _probe_focus(database: SourceDatabase, checked_at: str) -> list[dict[str, Any]]:
    db_rows: dict[str, list[dict[str, Any]]] = {}
    for row in database.get("focus_vintages", []):
        payload = _source_object(row.get("raw_value_json"))
        if payload is not None:
            db_rows.setdefault(str(row.get("series_key")), []).append({**payload, "_fetched_at": row.get("available_at")})
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
            filtered = [r for r in typed_records if r.get("Indicador") == indicator]
            if key == FOCUS_12M:
                filtered = [r for r in filtered if r.get("Suavizada") == "S" and r.get("baseCalculo") == 0]
            current: dict[tuple[str, str, str, str], dict[str, Any]] = {}
            for row in filtered:
                k = _focus_key(row)
                if k not in current or str(row.get("Data", "")) > str(current[k].get("Data", "")):
                    current[k] = row
            saved = {_focus_key(row): row for row in db_rows.get(key, [])}
            if not current:
                results.append({"identity": f"focus:{key}", "status": "invalid_source", "error_code": "empty_valid_observation_set", "source_checked_at": checked_at})
                continue
            for k, row in current.items():
                saved_row = saved.get(k)
                result = compare_observation(
                    str(row.get("Data")) if _date(row.get("Data")) else None,
                    row.get("Mediana"),
                    str(saved_row.get("Data")) if saved_row and _date(saved_row.get("Data")) else None,
                    saved_row.get("Mediana") if saved_row else None,
                    source_checked_at=checked_at,
                    db_fetched_at=saved_row.get("_fetched_at") if saved_row else None,
                    identity=f"focus:{key}:{'|'.join(k)}",
                )
                result["source_target"] = row.get("DataReferencia")
                if saved_row is None:
                    result["status"] = "database_missing"
                results.append(result)
        except Exception as exc:  # noqa: BLE001 - isolate failures per independently probed source.
            results.append({"identity": f"focus:{key}", "status": "source_unavailable", "error_code": type(exc).__name__, "source_checked_at": checked_at})
    return results


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
            data = _json(url, {"formato": "json"})
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
                fallback_selection = _sidra_rows(_json(fallback, {"formato": "json"}), expectations)
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
            results.append({"identity": f"ibge_sidra:{spec.key}", "status": "source_unavailable", "error_code": type(exc).__name__, "source_checked_at": checked_at})
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
        return [{"identity": "anbima:IMA-B", "status": "source_unavailable", "error_code": type(exc).__name__, "source_checked_at": checked_at}]


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
        if key not in latest or parsed > latest[key][0]:
            latest[key] = (parsed, row)
    return {key: row for key, (_day, row) in latest.items()}


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
        return [{"identity": "tesouro", "status": "source_unavailable", "error_code": type(exc).__name__, "source_checked_at": checked_at}]


def _parse_b3(data: bytes, identities: dict[str, dict[str, Any]]) -> list[dict[str, Any]]:
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
        return [{"identity": "b3:di", "status": "source_unavailable", "error_code": type(exc).__name__, "source_checked_at": checked_at}]


def run_audit(snapshot: dict[str, Any], *, clock: datetime | None = None) -> dict[str, Any]:
    checked_at = (clock or datetime.now(timezone.utc)).isoformat()
    database = _database_snapshot(snapshot.get("database"))
    groups: dict[str, list[dict[str, Any]]] = {
        "sgs": _probe_sgs(database, checked_at),
        "focus": _probe_focus(database, checked_at),
        "sidra": _probe_sidra(database, checked_at),
        "b3_di": _probe_b3(database, checked_at),
        "anbima": _probe_anbima(database, checked_at),
        "treasury": _probe_treasury(database, checked_at),
    }
    rows = [row for records in groups.values() for row in records]
    failures = {"source_newer", "source_unavailable", "invalid_source", "unsupported_coverage", "database_missing", "same_reference_value_mismatch", "database_ahead"}
    problems = sum(row.get("status") in failures for row in rows)
    expected_focus = {f"focus:{key}" for key in (*FOCUS_ANNUAL, FOCUS_12M)}
    focus_roots = {tuple(str(row.get("identity", "")).split(":", 2)[:2]) for row in groups["focus"]}
    expected_sidra = {f"ibge_sidra:{spec.key}" for spec in SIDRA_DATASETS.values()}
    coverage_complete = (
        {int(row["identity"].split(":", 1)[1]) for row in groups["sgs"] if str(row.get("identity", "")).startswith("bcb_sgs:")} == set(SGS_CODES)
        and {f"{root[0]}:{root[1]}" for root in focus_roots} == expected_focus
        and {str(row.get("identity")) for row in groups["sidra"]} == expected_sidra
        and bool(groups["b3_di"]) and not any(row.get("identity") == "b3:di" for row in groups["b3_di"])
        and bool(groups["anbima"]) and not any(str(row.get("status")) in {"source_unavailable", "invalid_source"} for row in groups["anbima"])
        and bool(groups["treasury"]) and not any(str(row.get("status")) in {"source_unavailable", "invalid_source"} for row in groups["treasury"])
        and not any(
            int(value or 0) > 0
            for limit in snapshot.get("database", {}).get("audit_limits", [])
            for value in limit.values()
        )
    )
    return {
        "schema_version": "source-audit-report-v1",
        "generated_at": checked_at,
        "database_snapshot_at": snapshot.get("captured_at"),
        "catalog_version": "source-audit-catalog-v1",
        "status": "healthy" if coverage_complete and problems == 0 else "degraded",
        "coverage": {"expected_static": 75, "checked": len(rows), "complete": coverage_complete,
                     "counts_by_source": {name: len(results) for name, results in groups.items()},
                     "database_limit_exceeded": any(
                         int(value or 0) > 0
                         for limit in snapshot.get("database", {}).get("audit_limits", [])
                         for value in limit.values()
                     )},
        "counts_by_status": {status: sum(row.get("status") == status for row in rows) for status in sorted({str(row.get("status")) for row in rows})},
        "sources": groups,
    }


def render_markdown(report: dict[str, Any]) -> str:
    lines = ["# X-BRAY — auditoria semanal de fontes", "", f"- Resultado: **{report['status']}**",
             f"- Verificado em: `{report['generated_at']}`", f"- Snapshot do banco: `{report.get('database_snapshot_at')}`",
             f"- Cobertura: {report['coverage']['checked']} itens; completa: {report['coverage']['complete']}", "",
             "| Identidade | Status | Referência da fonte | Valor da fonte | Referência do banco | Valor do banco |", "|---|---|---:|---:|---:|---:|"]
    for group in report["sources"].values():
        for row in group:
            lines.append("| {identity} | {status} | {source_reference} | {source_value} | {database_reference} | {database_value} |".format(
                identity=row.get("identity", "unknown"), status=row.get("status", "unknown"),
                source_reference=row.get("source_reference", "—"), source_value=row.get("source_value", "—"),
                database_reference=row.get("database_reference", "—"), database_value=row.get("database_value", "—"),
            ))
    return "\n".join(lines) + "\n"
