"""Explicit, append-only import of audited retrospective ledgers into PostgreSQL."""

from __future__ import annotations

import base64
import gzip
import hashlib
import json
import math
import os
import re
import sqlite3
import stat
import struct
from collections import defaultdict
from contextlib import closing
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal
from pathlib import Path
from typing import Any, cast
from urllib.parse import urlparse
from uuid import UUID

EXPECTED_TARGET = "xbray-api"
IMPORT_TARGET_ENV = "XBRAY_IMPORT_TARGET"
REQUIRED_MIGRATIONS = {"001_observability", "006_retrospective_macro_history",
                       "007_retrospective_di", "008_di_maturity_provenance",
                       "009_b3_instrument_snapshots", "010_production_data_imports"}
NON_DI_COMPONENTS = {
    "abs_focus_12m_target", "abs_ipca_12m_target", "cobertura_externa",
    "comprometimento_renda", "conta_corrente_12m_pib", "dbgg_pib", "dbgg_yoy_pp",
    "desemprego", "ibc_br_sa_yoy", "icc_pf", "inadimplencia_pf",
    "rendimento_real_yoy", "reservas_internacionais", "saldo_nominal_12m_pib",
    "saldo_primario_12m_pib", "spread_credito_pf_selic",
}
TABLES: dict[str, tuple[tuple[str, ...], tuple[str, ...]]] = {
    "retrospective_di_observations": (
        ("trade_date", "symbol", "maturity", "rate", "source_hash", "fetched_at", "revision",
         "source_basis", "source_name", "source_member", "maturity_basis", "security_id"),
        ("trade_date", "symbol", "source_hash", "source_member"),
    ),
    "b3_di_instrument_snapshots": (
        ("snapshot_date", "report_date", "symbol", "security_id", "maturity", "trading_start",
         "trading_end", "fin_instrm_id", "source_hash", "source_name", "message_definition",
         "message_id", "captured_at", "available_at", "source_basis"),
        ("source_hash", "symbol", "security_id", "maturity"),
    ),
    "retrospective_di_price_identities": (
        ("trade_date", "symbol", "source_hash", "source_member", "security_id", "captured_at",
         "source_basis"),
        ("trade_date", "symbol", "source_hash", "source_member"),
    ),
    "retrospective_macro_values": (
        ("component_key", "reference_month", "value", "unit", "formula_version", "source_basis",
         "fetched_at", "inputs_json", "inputs_hash", "reason"),
        ("component_key", "reference_month", "formula_version", "inputs_hash"),
    ),
    "observation_vintages": (
        ("provider", "series_key", "reference_period", "dimensions_json", "dimensions_hash",
         "raw_value_json", "source_published_at", "first_seen_at", "available_at", "payload_hash",
         "revision_id"),
        ("provider", "series_key", "reference_period", "dimensions_hash", "payload_hash"),
    ),
}
DATE_COLUMNS: dict[str, frozenset[str]] = {
    "retrospective_di_observations": frozenset({"trade_date", "maturity"}),
    "b3_di_instrument_snapshots": frozenset({
        "snapshot_date", "report_date", "maturity", "trading_start", "trading_end",
    }),
    "retrospective_di_price_identities": frozenset({"trade_date"}),
    "retrospective_macro_values": frozenset(),
    "observation_vintages": frozenset({"reference_period"}),
}
TIMESTAMP_COLUMNS: dict[str, frozenset[str]] = {
    "retrospective_di_observations": frozenset({"fetched_at"}),
    "b3_di_instrument_snapshots": frozenset({"captured_at", "available_at"}),
    "retrospective_di_price_identities": frozenset({"captured_at"}),
    "retrospective_macro_values": frozenset({"fetched_at"}),
    "observation_vintages": frozenset({"source_published_at", "first_seen_at", "available_at"}),
}
MONTH_COLUMNS: dict[str, frozenset[str]] = {
    "retrospective_macro_values": frozenset({"reference_month"}),
}
FLOAT_COLUMNS: dict[str, frozenset[str]] = {
    "retrospective_di_observations": frozenset({"rate"}),
    "retrospective_macro_values": frozenset({"value"}),
}
JSON_TEXT_COLUMNS: dict[str, frozenset[str]] = {
    "retrospective_macro_values": frozenset({"inputs_json"}),
    "observation_vintages": frozenset({"dimensions_json", "raw_value_json"}),
}
TEXT_TYPES = frozenset({"text", "character varying", "character"})
DATE_TYPES = frozenset({"date"})
TIMESTAMP_TYPES = frozenset({"timestamp with time zone", "timestamp without time zone"})
FLOAT_TYPES = frozenset({"real", "double precision", "numeric", "decimal"})
FOCUS_KEY = "IPCA_12M_S_BASE0"
BACKUP_TYPE_KEY = "__xbray_backup_type__"


def _canonical(value: object) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False,
                      allow_nan=False)


def _sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _file_hash(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        while chunk := stream.read(1024 * 1024):
            digest.update(chunk)
    return digest.hexdigest()


def _backup_encode(value: object) -> object:
    """Encode supported PostgreSQL row types without conflating them with text."""
    if value is None or isinstance(value, (str, bool, int)):
        return value
    if isinstance(value, float):
        if not math.isfinite(value):
            raise ValueError("non-finite float cannot be written to backup")
        return value
    if isinstance(value, datetime):
        return {BACKUP_TYPE_KEY: "datetime", "value": value.isoformat(), "fold": value.fold}
    if isinstance(value, date):
        return {BACKUP_TYPE_KEY: "date", "value": value.isoformat()}
    if isinstance(value, Decimal):
        if not value.is_finite():
            raise ValueError("non-finite Decimal cannot be written to backup")
        return {BACKUP_TYPE_KEY: "decimal", "value": str(value)}
    if isinstance(value, timedelta):
        return {BACKUP_TYPE_KEY: "timedelta", "days": value.days,
                "seconds": value.seconds, "microseconds": value.microseconds}
    if isinstance(value, UUID):
        return {BACKUP_TYPE_KEY: "uuid", "value": str(value)}
    if isinstance(value, memoryview):
        return {BACKUP_TYPE_KEY: "memoryview", "base64": base64.b64encode(value.tobytes()).decode("ascii")}
    if isinstance(value, bytes):
        return {BACKUP_TYPE_KEY: "bytes", "base64": base64.b64encode(value).decode("ascii")}
    if isinstance(value, dict):
        mapping = cast(dict[object, object], value)
        if any(not isinstance(key, str) for key in mapping):
            raise TypeError("backup mapping keys must be strings")
        string_mapping = cast(dict[str, object], mapping)
        return {
            BACKUP_TYPE_KEY: "mapping",
            "items": [[key, _backup_encode(item)] for key, item in string_mapping.items()],
        }
    if isinstance(value, list):
        values = cast(list[object], value)
        return {BACKUP_TYPE_KEY: "list", "items": [_backup_encode(item) for item in values]}
    if isinstance(value, tuple):
        values = cast(tuple[object, ...], value)
        return {BACKUP_TYPE_KEY: "tuple", "items": [_backup_encode(item) for item in values]}
    raise TypeError(f"unsupported PostgreSQL backup type: {type(value).__name__}")


def _backup_decode(value: object) -> object:
    """Decode versioned tagged values emitted by `_backup_encode` for audits/tests."""
    if isinstance(value, list):
        return [_backup_decode(item) for item in cast(list[object], value)]
    if not isinstance(value, dict):
        return value
    mapping = cast(dict[str, object], value)
    kind = mapping.get(BACKUP_TYPE_KEY)
    if kind == "mapping":
        items = mapping.get("items")
        if not isinstance(items, list):
            raise ValueError("invalid backup mapping payload")
        decoded: dict[str, object] = {}
        for item in cast(list[object], items):
            if not isinstance(item, list):
                raise TypeError("invalid backup mapping entry")
            pair = cast(list[object], item)
            if len(pair) != 2 or not isinstance(pair[0], str):
                raise ValueError("invalid backup mapping entry")
            decoded[pair[0]] = _backup_decode(pair[1])
        return decoded
    if kind in {"list", "tuple"}:
        items = mapping.get("items")
        if not isinstance(items, list):
            raise ValueError("invalid backup sequence payload")
        decoded_items = [_backup_decode(item) for item in cast(list[object], items)]
        return tuple(decoded_items) if kind == "tuple" else decoded_items
    if kind == "datetime":
        parsed = datetime.fromisoformat(str(mapping["value"]))
        return parsed.replace(fold=int(cast(int | str, mapping.get("fold", 0))))
    if kind == "date":
        return date.fromisoformat(str(mapping["value"]))
    if kind == "decimal":
        return Decimal(str(mapping["value"]))
    if kind == "timedelta":
        return timedelta(days=int(cast(int | str, mapping["days"])),
                         seconds=int(cast(int | str, mapping["seconds"])),
                         microseconds=int(cast(int | str, mapping["microseconds"])))
    if kind == "uuid":
        return UUID(str(mapping["value"]))
    if kind in {"bytes", "memoryview"}:
        raw = base64.b64decode(str(mapping["base64"]), validate=True)
        return memoryview(raw) if kind == "memoryview" else raw
    if kind is not None:
        raise ValueError("unknown tagged value in production backup")
    return {key: _backup_decode(item) for key, item in mapping.items()}


def _read_source(path: Path, table: str, where: str = "", params: tuple[object, ...] = ()) -> list[dict[str, object]]:
    if not path.is_absolute() or path.suffix != ".db" or not path.is_file():
        raise ValueError("source databases must be existing absolute .db paths")
    try:
        with closing(sqlite3.connect(f"{path.as_uri()}?mode=ro", uri=True)) as conn:
            conn.row_factory = sqlite3.Row
            columns = {str(row[1]) for row in conn.execute(f'PRAGMA table_info("{table}")')}
            if not columns:
                return []
            query = f'SELECT * FROM "{table}" {where}'
            return [dict(row) for row in conn.execute(query, params).fetchall()]
    except sqlite3.Error as error:
        raise ValueError(f"could not read source table {table}") from error


def _source_bundle(di_db: Path, non_di_db: Path, focus_db: Path) -> tuple[dict[str, list[dict[str, object]]], dict[str, object]]:
    from macro_score.retrospective import reconstruct_di_ledger

    rows = {
        "retrospective_di_observations": _read_source(di_db, "retrospective_di_observations"),
        "b3_di_instrument_snapshots": _read_source(di_db, "b3_di_instrument_snapshots"),
        "retrospective_di_price_identities": _read_source(di_db, "retrospective_di_price_identities"),
        "retrospective_macro_values": (
            _read_source(di_db, "retrospective_macro_values")
            + _read_source(non_di_db, "retrospective_macro_values")
        ),
        "observation_vintages": _read_source(
            focus_db, "observation_vintages", "WHERE provider = ? AND series_key = ?",
            ("focus", FOCUS_KEY),
        ),
    }
    source_files = {
        "di_ledger": _file_hash(di_db),
        "non_di_macro_ledger": _file_hash(non_di_db),
        "focus_vintages": _file_hash(focus_db),
    }
    reconstruction = reconstruct_di_ledger(
        di_db, as_of=datetime.now(timezone.utc), focus_sqlite=focus_db,
    )
    verified_di_months = reconstruction.get("eligible_distinct_months")
    verified_counts = cast(dict[str, object], verified_di_months) if isinstance(verified_di_months, dict) else {}
    if any(
        not isinstance(verified_counts.get(key), int) or cast(int, verified_counts[key]) < 24
        for key in ("juro_real_ex_ante_1y", "inclinacao_di_5y_1y")
    ):
        raise ValueError("source DI ledger fails the maturity/identity 24-month gate")
    manifest: dict[str, object] = {
        "manifest_version": "xbray-retrospective-production-import-v1",
        "source_files_sha256": source_files,
        "row_counts": {table: len(table_rows) for table, table_rows in rows.items()},
        "verified_di_distinct_months": verified_counts,
        "point_in_time": False,
    }
    digest_payload = {table: sorted(table_rows, key=_canonical) for table, table_rows in rows.items()}
    manifest["total_rows"] = sum(len(table_rows) for table_rows in rows.values())
    manifest["bundle_sha256"] = _sha256(_canonical({
        "manifest": manifest, "rows": digest_payload,
    }).encode())
    return rows, manifest


def _aware_timestamp(value: object, *, field: str, now: datetime) -> datetime:
    if isinstance(value, datetime):
        parsed = value
    elif isinstance(value, str):
        try:
            parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
        except ValueError as error:
            raise ValueError(f"invalid {field} timestamp") from error
    else:
        raise TypeError(f"invalid {field} timestamp")
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise ValueError(f"timezone missing from {field}")
    if parsed.astimezone(timezone.utc) > now:
        raise ValueError(f"future {field} timestamp rejected")
    return parsed.astimezone(timezone.utc)


def _is_digest(value: object) -> bool:
    if not isinstance(value, str) or len(value) != 64:
        return False
    try:
        int(value, 16)
    except ValueError:
        return False
    return True


def _validate_macro_rows(macros: list[dict[str, object]], now: datetime) -> dict[str, set[str]]:
    component_months: dict[str, set[str]] = defaultdict(set)
    for row in macros:
        if row.get("source_basis") != "retrospective_reference":
            raise ValueError("non-retrospective macro rows are not importable")
        component, month = row.get("component_key"), row.get("reference_month")
        if not isinstance(component, str) or not isinstance(month, str):
            raise TypeError("invalid retrospective macro key")
        expected_formula = (
            "macro-retrospective-di-2.0.0"
            if component in {"juro_real_ex_ante_1y", "inclinacao_di_5y_1y"}
            else "macro-retrospective-1.0.0"
        )
        if row.get("formula_version") != expected_formula:
            raise ValueError("unexpected retrospective formula version")
        if component not in NON_DI_COMPONENTS | {
            "distancia_focus_meta", "variacao_saldo_primario_12m",
            "juro_real_ex_ante_1y", "inclinacao_di_5y_1y",
        }:
            raise ValueError("unexpected retrospective component")
        _aware_timestamp(row.get("fetched_at"), field="fetched_at", now=now)
        inputs_text, inputs_hash = row.get("inputs_json"), row.get("inputs_hash")
        if not isinstance(inputs_text, str) or not _is_digest(inputs_hash):
            raise ValueError("invalid retrospective macro provenance")
        try:
            inputs = json.loads(inputs_text, parse_constant=lambda _value: (_ for _ in ()).throw(ValueError()))
        except (ValueError, json.JSONDecodeError) as error:
            raise ValueError("invalid retrospective input payload") from error
        if _sha256(_canonical(inputs).encode()) != inputs_hash:
            raise ValueError("retrospective input hash mismatch")
        value = row.get("value")
        if value is not None and (isinstance(value, bool) or not isinstance(value, (int, float))
                                  or not math.isfinite(float(value))):
            raise ValueError("non-finite retrospective value")
        if value is not None:
            component_months[component].add(month)
    if not NON_DI_COMPONENTS.issubset(component_months):
        raise ValueError("source bundle does not cover all non-DI Macro Score components")
    if any(len(component_months[key]) < 24 for key in NON_DI_COMPONENTS):
        raise ValueError("non-DI source coverage below the 24-month operational gate")
    return component_months


def _validate_registry(registry: list[dict[str, object]], now: datetime) -> None:
    identity: dict[tuple[str, str, str], set[str]] = defaultdict(set)
    for row in registry:
        if row.get("source_basis") != "b3_instrument_snapshot_v1" or row.get("message_definition") != "bvmf.100.02":
            raise ValueError("unverified B3 instrument catalog source")
        if not _is_digest(row.get("source_hash")) or not str(row.get("symbol", "")).startswith("DI1"):
            raise ValueError("invalid B3 instrument identity")
        _aware_timestamp(row.get("captured_at"), field="captured_at", now=now)
        _aware_timestamp(row.get("available_at"), field="available_at", now=now)
        key = (str(row.get("report_date")), str(row.get("symbol")), str(row.get("security_id")))
        identity[key].add(str(row.get("maturity")))
    if any(len(maturities) > 1 for maturities in identity.values()):
        raise ValueError("conflicting B3 instrument snapshot maturity")


def _validate_raw_di(rows: list[dict[str, object]], now: datetime) -> None:
    for row in rows:
        if row.get("source_basis") != "retrospective_reference" or not _is_digest(row.get("source_hash")):
            raise ValueError("invalid retrospective DI observation")
        rate = row.get("rate")
        if isinstance(rate, bool) or not isinstance(rate, (int, float)) or not math.isfinite(float(rate)):
            raise ValueError("invalid retrospective DI rate")
        _aware_timestamp(row.get("fetched_at"), field="fetched_at", now=now)


def _validate_price_identities(rows: list[dict[str, object]], now: datetime) -> None:
    for row in rows:
        if row.get("source_basis") != "b3_price_identity_v1" or not _is_digest(row.get("source_hash")):
            raise ValueError("invalid B3 price identity crosswalk")
        _aware_timestamp(row.get("captured_at"), field="captured_at", now=now)


def _validate_vintages(rows: list[dict[str, object]], now: datetime) -> None:
    for row in rows:
        if row.get("provider") != "focus" or row.get("series_key") != FOCUS_KEY:
            raise ValueError("unexpected vintage provider or series")
        _aware_timestamp(row.get("available_at"), field="available_at", now=now)
        _aware_timestamp(row.get("first_seen_at"), field="first_seen_at", now=now)
        for key in ("payload_hash", "dimensions_hash"):
            if not _is_digest(row.get(key)):
                raise ValueError("invalid Focus vintage hash")
        try:
            raw = json.loads(str(row["raw_value_json"]))
            dimensions = json.loads(str(row["dimensions_json"]))
        except (KeyError, ValueError, json.JSONDecodeError) as error:
            raise ValueError("invalid Focus vintage JSON") from error
        if (_sha256(_canonical(raw).encode()) != row.get("payload_hash")
                or _sha256(_canonical(dimensions).encode()) != row.get("dimensions_hash")
                or row.get("revision_id") != str(row.get("payload_hash"))[:16]):
            raise ValueError("Focus vintage integrity check failed")


def _validate_di_coverage(macros: list[dict[str, object]]) -> dict[str, set[str]]:
    di_coverage: dict[str, set[str]] = defaultdict(set)
    for row in macros:
        key = row.get("component_key")
        if key in {"juro_real_ex_ante_1y", "inclinacao_di_5y_1y"} and row.get("value") is not None:
            di_coverage[str(key)].add(str(row["reference_month"]))
    if any(len(di_coverage.get(key, set())) < 24 for key in ("juro_real_ex_ante_1y", "inclinacao_di_5y_1y")):
        raise ValueError("verified DI retrospective coverage below 24 months")
    return di_coverage


def _validate_bundle(rows: dict[str, list[dict[str, object]]], now: datetime) -> dict[str, object]:
    if any(not rows[table] for table in TABLES):
        raise ValueError("source bundle is missing one or more required ledgers")
    macros = rows["retrospective_macro_values"]
    component_months = _validate_macro_rows(macros, now)
    _validate_registry(rows["b3_di_instrument_snapshots"], now)
    _validate_raw_di(rows["retrospective_di_observations"], now)
    _validate_price_identities(rows["retrospective_di_price_identities"], now)
    _validate_vintages(rows["observation_vintages"], now)
    di_coverage = _validate_di_coverage(macros)
    return {
        "coverage_distinct_months": {key: len(value) for key, value in sorted(component_months.items())},
        "di_distinct_months": {key: len(value) for key, value in sorted(di_coverage.items())},
        "point_in_time": False,
    }


def _table_rows(conn: Any, table: str, columns: tuple[str, ...]) -> list[dict[str, object]]:
    query = f'SELECT {", ".join(columns)} FROM "{table}"'
    with conn.cursor() as cursor:
        cursor.execute(query)
        names = [description.name if hasattr(description, "name") else description[0]
                 for description in cursor.description]
        return [dict(zip(names, row, strict=True)) if not isinstance(row, dict) else row
                for row in cursor.fetchall()]


def _semantic_timestamp(value: object, table: str, column: str) -> str | None:
    if value is None:
        return None
    if isinstance(value, datetime):
        parsed = value
    elif isinstance(value, str):
        try:
            parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
        except ValueError as error:
            raise ValueError(f"invalid {table}.{column} timestamp") from error
    else:
        raise TypeError(f"unsupported {table}.{column} timestamp type")
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise ValueError(f"timezone missing from {table}.{column}")
    return parsed.astimezone(timezone.utc).isoformat(timespec="microseconds")


def _semantic_date(value: object, table: str, column: str) -> str | None:
    if value is None:
        return None
    if isinstance(value, datetime):
        raise TypeError(f"datetime is not a date for {table}.{column}")
    if isinstance(value, date):
        return value.isoformat()
    if not isinstance(value, str):
        raise TypeError(f"unsupported {table}.{column} date type")
    try:
        parsed = date.fromisoformat(value)
    except ValueError as error:
        raise ValueError(f"invalid {table}.{column} date") from error
    if parsed.isoformat() != value:
        raise ValueError(f"non-canonical {table}.{column} date")
    return parsed.isoformat()


def _decimal_canonical(value: Decimal) -> str:
    if not value.is_finite():
        raise ValueError("non-finite numeric value")
    decimal_tuple = value.as_tuple()
    sign = decimal_tuple.sign
    exponent_value = decimal_tuple.exponent
    if not isinstance(exponent_value, int):
        raise TypeError("non-finite numeric value")
    exponent = exponent_value
    significant = list(decimal_tuple.digits)
    while significant and significant[-1] == 0:
        significant.pop()
        exponent += 1
    if not significant:
        return "0"
    normalized = Decimal((sign, tuple(significant), exponent))
    return str(normalized)


def _semantic_float(value: object, table: str, column: str,
                    pg_type: str = "double precision") -> dict[str, str] | None:
    if value is None:
        return None
    if isinstance(value, bool):
        raise TypeError(f"boolean is not numeric for {table}.{column}")
    if pg_type in {"numeric", "decimal"}:
        if isinstance(value, bool) or not isinstance(value, (int, float, Decimal)):
            raise TypeError(f"unsupported {table}.{column} numeric type")
        decimal_value = value if isinstance(value, Decimal) else Decimal(str(value))
        return {"numeric": _decimal_canonical(decimal_value)}
    if pg_type == "real":
        if isinstance(value, Decimal) or isinstance(value, (int, float)) and not isinstance(value, bool):
            decimal_value = float(value)
        else:
            raise TypeError(f"unsupported {table}.{column} numeric type")
        if not math.isfinite(decimal_value):
            raise ValueError(f"non-finite {table}.{column}")
        rounded = struct.unpack("!f", struct.pack("!f", decimal_value))[0]
        return {"real32_hex": rounded.hex()}
    if pg_type == "double precision" and isinstance(value, Decimal):
        value = float(value)
        if not math.isfinite(value):
            raise ValueError(f"out-of-range {table}.{column} double precision")
    if isinstance(value, float):
        if not math.isfinite(value):
            raise ValueError(f"non-finite {table}.{column}")
        return {"float64_hex": value.hex()}
    if isinstance(value, Decimal):
        if not value.is_finite():
            raise ValueError(f"non-finite {table}.{column}")
        as_float = float(value)
        if math.isfinite(as_float) and Decimal.from_float(as_float) == value:
            return {"float64_hex": as_float.hex()}
        return {"decimal_exact": str(value)}
    if isinstance(value, int):
        return {"integer_exact": str(value)}
    raise TypeError(f"unsupported {table}.{column} numeric type")


def _semantic_value(table: str, column: str, value: object,
                    column_types: dict[tuple[str, str], str] | None = None) -> object:
    pg_type = (column_types or {}).get((table, column), "")
    if column in DATE_COLUMNS.get(table, frozenset()):
        return _semantic_date(value, table, column)
    if column in TIMESTAMP_COLUMNS.get(table, frozenset()):
        return _semantic_timestamp(value, table, column)
    if column in MONTH_COLUMNS.get(table, frozenset()):
        if not isinstance(value, str) or re.fullmatch(r"\d{4}-(0[1-9]|1[0-2])", value) is None:
            raise ValueError(f"invalid {table}.{column} month")
        return value
    if column in FLOAT_COLUMNS.get(table, frozenset()):
        return _semantic_float(value, table, column, pg_type or "double precision")
    if column in JSON_TEXT_COLUMNS.get(table, frozenset()):
        if not isinstance(value, str):
            raise TypeError(f"unsupported {table}.{column} JSON storage type")
        try:
            parsed = json.loads(value, parse_constant=lambda _item: (_ for _ in ()).throw(ValueError()))
        except (ValueError, json.JSONDecodeError) as error:
            raise ValueError(f"invalid {table}.{column} JSON") from error
        return _canonical(parsed)
    if column_types is not None and pg_type not in TEXT_TYPES | DATE_TYPES | TIMESTAMP_TYPES:
        raise ValueError(f"unsupported target schema type for {table}.{column}: {pg_type}")
    if value is None or isinstance(value, (str, bool, int)):
        return value
    raise TypeError(f"unsupported {table}.{column} schema type: {type(value).__name__}")


def _load_column_types(conn: Any) -> dict[tuple[str, str], str]:
    """Read and validate target types once, before planning, backup, or inserts."""
    wanted = {(table, column) for table, (columns, _keys) in TABLES.items() for column in columns}
    with conn.cursor() as cursor:
        cursor.execute(
            "SELECT table_name,column_name,data_type FROM information_schema.columns "
            "WHERE table_schema=current_schema() AND table_name = ANY(%s)",
            (list(TABLES),),
        )
        descriptions = cast(list[Any], cursor.description)
        names = [item.name if hasattr(item, "name") else item[0] for item in descriptions]
        found: dict[tuple[str, str], str] = {}
        raw_rows = cast(list[object], cursor.fetchall())
        for raw in raw_rows:
            if isinstance(raw, dict):
                record = cast(dict[str, object], raw)
            elif isinstance(raw, (tuple, list)):
                record = dict(zip(names, cast(tuple[Any, ...] | list[Any], raw), strict=True))
            else:
                raise TypeError("unexpected PostgreSQL schema metadata row")
            table, column, pg_type = record["table_name"], record["column_name"], record["data_type"]
            key = (str(table), str(column))
            if key in wanted:
                found[key] = str(pg_type).lower()
    return _validate_column_types(found)


def _validate_column_types(found: dict[tuple[str, str], str]) -> dict[tuple[str, str], str]:
    """Validate schema metadata supplied by a PostgreSQL cursor or admin transport."""
    wanted = {(table, column) for table, (columns, _keys) in TABLES.items() for column in columns}
    normalized = {key: value.lower() for key, value in found.items() if key in wanted}
    missing = wanted - normalized.keys()
    if missing:
        raise ValueError("target PostgreSQL schema is missing required import columns")
    for table, column in wanted:
        pg_type = normalized[(table, column)]
        if column in FLOAT_COLUMNS.get(table, frozenset()):
            supported = FLOAT_TYPES
        elif column in DATE_COLUMNS.get(table, frozenset()):
            supported = TEXT_TYPES | DATE_TYPES
        elif column in TIMESTAMP_COLUMNS.get(table, frozenset()):
            supported = TEXT_TYPES | frozenset({"timestamp with time zone"})
        else:
            supported = TEXT_TYPES
        if pg_type not in supported:
            raise ValueError(f"unsupported target schema type for {table}.{column}: {pg_type}")
    return normalized


def validate_column_types(found: dict[tuple[str, str], str]) -> dict[tuple[str, str], str]:
    """Validate PostgreSQL column metadata for an authorized transport adapter."""
    return _validate_column_types(found)


def bind_value(table: str, column: str, value: object,
               column_types: dict[tuple[str, str], str]) -> object:
    """Normalize a source value to the native target PostgreSQL type."""
    return _bind_value(table, column, value, column_types)


def semantic_identity(table: str, row: dict[str, object], keys: tuple[str, ...],
                      column_types: dict[tuple[str, str], str] | None = None) -> str:
    """Return the validated importer's canonical typed identity."""
    return _semantic_identity(table, row, keys, column_types)


def semantic_row_hash(table: str, row: dict[str, object],
                      column_types: dict[tuple[str, str], str] | None = None) -> str:
    """Return the validated importer's canonical typed row digest."""
    return _semantic_row_hash(table, row, column_types)


def source_bundle(di_db: Path, non_di_db: Path, focus_db: Path) -> tuple[dict[str, list[dict[str, object]]], dict[str, object]]:
    """Load source ledgers and construct their proof-carrying canonical manifest."""
    return _source_bundle(di_db, non_di_db, focus_db)


def validate_bundle(rows: dict[str, list[dict[str, object]]], now: datetime) -> dict[str, object]:
    """Run the authoritative source, vintage, maturity and timestamp validators."""
    return _validate_bundle(rows, now)


def plan_import(rows: dict[str, list[dict[str, object]]],
                existing: dict[str, list[dict[str, object]]],
                column_types: dict[tuple[str, str], str]) -> dict[str, list[dict[str, object]]]:
    """Plan append-only additions using the authoritative semantic comparisons."""
    return _plan(rows, existing, column_types)


def canonical_json(value: object) -> str:
    """Serialize using the importer's canonical JSON contract."""
    return _canonical(value)


def encode_backup(tables: dict[str, list[dict[str, object]]]) -> dict[str, object]:
    """Preserve typed ledger values using backup format version 2 semantics."""
    encoded = _backup_encode(tables)
    if not isinstance(encoded, dict):
        raise TypeError("backup encoder returned an invalid envelope")
    return cast(dict[str, object], encoded)


def _bind_value(table: str, column: str, value: object,
                column_types: dict[tuple[str, str], str]) -> object:
    pg_type = column_types[(table, column)]
    if value is None:
        return None
    if column in FLOAT_COLUMNS.get(table, frozenset()):
        if pg_type in {"numeric", "decimal"}:
            if isinstance(value, Decimal):
                return value
            if isinstance(value, bool) or not isinstance(value, (float, int)):
                raise TypeError(f"unsupported {table}.{column} source numeric")
            return Decimal(str(value))
        if isinstance(value, bool) or not isinstance(value, (float, int, Decimal)):
            raise TypeError(f"unsupported {table}.{column} source numeric")
        return float(value)
    if column in DATE_COLUMNS.get(table, frozenset()) and pg_type == "date":
        return date.fromisoformat(value) if isinstance(value, str) else value
    if column in TIMESTAMP_COLUMNS.get(table, frozenset()) and pg_type == "timestamp with time zone":
        if isinstance(value, str):
            parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
            if parsed.tzinfo is None or parsed.utcoffset() is None:
                raise ValueError(f"timezone missing from {table}.{column}")
            return parsed.astimezone(timezone.utc)
        return value
    if column in JSON_TEXT_COLUMNS.get(table, frozenset()):
        if not isinstance(value, str):
            raise TypeError(f"unsupported {table}.{column} JSON source type")
        return _canonical(json.loads(value))
    return value


def _semantic_row(table: str, row: dict[str, object],
                  column_types: dict[tuple[str, str], str] | None = None) -> dict[str, object]:
    columns = TABLES[table][0]
    return {
        column: _semantic_value(table, column, row.get(column), column_types)
        for column in columns
    }


def _semantic_identity(table: str, row: dict[str, object], keys: tuple[str, ...],
                       column_types: dict[tuple[str, str], str] | None = None) -> str:
    semantic = _semantic_row(table, row, column_types)
    return _canonical({key: semantic[key] for key in keys})


def _semantic_row_hash(table: str, row: dict[str, object],
                       column_types: dict[tuple[str, str], str] | None = None) -> str:
    return _sha256(_canonical(_semantic_row(table, row, column_types)).encode())


def _identity(row: dict[str, object], keys: tuple[str, ...], table: str | None = None,
              column_types: dict[tuple[str, str], str] | None = None) -> str:
    if table is None:
        return _canonical({key: row[key] for key in keys})
    return _semantic_identity(table, row, keys, column_types)


def _first_value(row: object | None) -> object | None:
    if row is None:
        return None
    if isinstance(row, dict):
        values = cast(dict[str, object], row)
        return next(iter(values.values()), None)
    if isinstance(row, (tuple, list)):
        values = cast(tuple[Any, ...] | list[Any], row)
        return values[0] if values else None
    raise ValueError("unexpected PostgreSQL result row shape")


def _row_values(row: object, first: str, second: str) -> tuple[object, object]:
    if isinstance(row, dict):
        values = cast(dict[str, object], row)
        return values[first], values[second]
    if isinstance(row, (tuple, list)):
        values = cast(tuple[Any, ...] | list[Any], row)
        if len(values) >= 2:
            return values[0], values[1]
    raise ValueError("unexpected PostgreSQL journal row shape")


def _journal_values(row: object) -> tuple[object, object, object]:
    if isinstance(row, dict):
        values = cast(dict[str, object], row)
        return values["table_name"], values["row_identity_json"], values["row_sha256"]
    if isinstance(row, (tuple, list)):
        values = cast(tuple[Any, ...] | list[Any], row)
        if len(values) == 3:
            return values[0], values[1], values[2]
    raise ValueError("unexpected PostgreSQL batch journal row shape")



def plan_import_reusing_equivalent_vintages(
    rows: dict[str, list[dict[str, object]]],
    existing: dict[str, list[dict[str, object]]],
    column_types: dict[tuple[str, str], str],
    *,
    allow_reuse: bool,
    now: datetime,
) -> tuple[dict[str, list[dict[str, object]]], list[dict[str, object]]]:
    """Plan additions, optionally reusing only exact Focus vintages with differing capture times.

    Existing production rows remain untouched and are never journaled as rows owned
    by this import. All non-time semantic columns must match exactly.
    """
    plan: dict[str, list[dict[str, object]]] = {}
    reused: list[dict[str, object]] = []

    def timestamp_claim(value: object) -> str:
        if isinstance(value, datetime):
            return value.isoformat()
        if isinstance(value, str):
            return value
        raise TypeError("invalid vintage timestamp claim")

    for table, (columns, keys) in TABLES.items():
        old = {
            _identity(row, keys, table, column_types): row for row in existing[table]
        }
        pending: dict[str, dict[str, object]] = {}
        for source in rows[table]:
            clean = {column: source.get(column) for column in columns}
            identity = _identity(clean, keys, table, column_types)
            target = old.get(identity)
            if target is not None:
                if _semantic_row(table, target, column_types) == _semantic_row(
                    table, clean, column_types
                ):
                    continue
                if not allow_reuse or table != "observation_vintages":
                    raise ValueError(f"conflicting existing append-only row in {table}")
                time_fields = {"first_seen_at", "available_at"}
                source_semantic = _semantic_row(table, clean, column_types)
                target_semantic = _semantic_row(table, target, column_types)
                differences = {
                    field
                    for field in columns
                    if source_semantic[field] != target_semantic[field]
                }
                if not differences or not differences.issubset(time_fields):
                    raise ValueError(f"conflicting existing append-only row in {table}")
                source_times = {
                    field: timestamp_claim(clean[field])
                    for field in sorted(time_fields)
                }
                target_times = {
                    field: timestamp_claim(target[field])
                    for field in sorted(time_fields)
                }
                for field in time_fields:
                    _aware_timestamp(clean[field], field=field, now=now)
                    _aware_timestamp(target[field], field=field, now=now)
                reused.append(
                    {
                        "table": table,
                        "identity": identity,
                        "payload_hash": clean["payload_hash"],
                        "source_row_sha256": _semantic_row_hash(
                            table, clean, column_types
                        ),
                        "target_row_sha256": _semantic_row_hash(
                            table, target, column_types
                        ),
                        "timestamp_fields": sorted(differences),
                        "source_timestamps": source_times,
                        "target_timestamps": target_times,
                        "effective_available_at": max(
                            _aware_timestamp(
                                clean["available_at"], field="available_at", now=now
                            ),
                            _aware_timestamp(
                                target["available_at"], field="available_at", now=now
                            ),
                        ).isoformat(),
                        "reuse_policy": "preserve_exact_production_vintage_provenance_v1",
                    }
                )
                continue
            if identity in pending and _semantic_row(
                table, pending[identity], column_types
            ) != _semantic_row(table, clean, column_types):
                raise ValueError(f"conflicting duplicate source row in {table}")
            pending[identity] = clean
        plan[table] = list(pending.values())
    return plan, reused


def rebase_vintage_dependencies(
    rows: dict[str, list[dict[str, object]]],
    reused: list[dict[str, object]],
) -> int:
    """Raise imported macro dependency availability to the later recorded vintage time."""
    effective_values: dict[str, datetime] = {}
    for item in reused:
        payload_hash = str(item["payload_hash"])
        stamp = datetime.fromisoformat(str(item["effective_available_at"]))
        effective_values[payload_hash] = max(
            effective_values.get(payload_hash, stamp), stamp
        )
    effective = {key: value.isoformat() for key, value in effective_values.items()}
    if not effective:
        return 0

    def visit(value: object, stamps: list[datetime], changed_times: list[bool]) -> bool:
        found = False
        if isinstance(value, dict):
            mapping = cast(dict[str, object], value)
            payload_hash = mapping.get("payload_hash")
            if isinstance(payload_hash, str) and payload_hash in effective:
                existing = _aware_timestamp(
                    mapping.get("available_at"),
                    field="dependency available_at",
                    now=datetime.now(timezone.utc),
                )
                imported = datetime.fromisoformat(effective[payload_hash])
                safe = max(existing, imported)
                if imported > existing:
                    mapping["available_at"] = safe.isoformat()
                    changed_times.append(True)
                stamps.append(safe)
                found = True
            for child in mapping.values():
                found = visit(child, stamps, changed_times) or found
        elif isinstance(value, list):
            sequence = cast(list[object], value)
            for child in sequence:
                found = visit(child, stamps, changed_times) or found
        return found

    changed = 0
    for row in rows["retrospective_macro_values"]:
        raw_inputs = row.get("inputs_json")
        if not isinstance(raw_inputs, str):
            continue
        inputs = json.loads(raw_inputs)
        stamps: list[datetime] = []
        changed_times: list[bool] = []
        if not visit(inputs, stamps, changed_times):
            continue
        fetched = _aware_timestamp(
            row.get("fetched_at"), field="fetched_at", now=datetime.now(timezone.utc)
        )
        effective_fetched = max([fetched, *stamps])
        if effective_fetched > fetched:
            row["fetched_at"] = effective_fetched.isoformat()
            changed_times.append(True)
        if changed_times:
            row["inputs_json"] = _canonical(inputs)
            row["inputs_hash"] = _sha256(str(row["inputs_json"]).encode())
            changed += 1
    return changed


def _select_identity_row(conn: Any, table: str, columns: tuple[str, ...],
                         keys: tuple[str, ...], identity: dict[str, object],
                         column_types: dict[tuple[str, str], str] | None = None) -> dict[str, object] | None:
    where = " AND ".join(f'"{key}"=%s' for key in keys)
    query = f'SELECT {", ".join(columns)} FROM "{table}" WHERE {where}'
    with conn.cursor() as cursor:
        params = tuple(_bind_value(table, key, identity[key], column_types)
                       if column_types is not None else identity[key] for key in keys)
        cursor.execute(query, params)
        row = cursor.fetchone()
        if row is None:
            return None
        if isinstance(row, dict):
            return cast(dict[str, object], row)
        names = [description.name if hasattr(description, "name") else description[0]
                 for description in cursor.description]
        return dict(zip(names, row, strict=True))


def _check_migrations(conn: Any) -> None:
    with conn.cursor() as cursor:
        cursor.execute("SELECT version FROM schema_migrations")
        versions = {str(_first_value(row)) for row in cursor.fetchall()}
    if not REQUIRED_MIGRATIONS.issubset(versions):
        raise ValueError("production database migrations are incomplete; deploy migration first")


def _backup_tables(conn: Any, destination: Path, repo_root: Path) -> str:
    target = destination.resolve()
    if not destination.is_absolute() or target.is_relative_to(repo_root.resolve()):
        raise ValueError("backup path must be absolute and outside the repository")
    if target.exists():
        raise ValueError("backup path already exists; refusing overwrite")
    backup = {table: _table_rows(conn, table, columns) for table, (columns, _keys) in TABLES.items()}
    payload = _canonical({
        "format": "xbray-production-ledger-backup-v1",
        "format_version": 2,
        "tables": _backup_encode(backup),
    }).encode()
    target.parent.mkdir(parents=True, exist_ok=True)
    descriptor = os.open(target, os.O_WRONLY | os.O_CREAT | os.O_EXCL, stat.S_IRUSR | stat.S_IWUSR)
    with os.fdopen(descriptor, "wb") as raw, gzip.GzipFile(fileobj=raw, mode="wb", mtime=0) as stream:
        stream.write(payload)
    return _sha256(payload)


def _plan(rows: dict[str, list[dict[str, object]]], existing: dict[str, list[dict[str, object]]],
          column_types: dict[tuple[str, str], str] | None = None) -> dict[str, list[dict[str, object]]]:
    planned: dict[str, list[dict[str, object]]] = {}
    for table, (columns, keys) in TABLES.items():
        old = {_identity(row, keys, table, column_types): row for row in existing[table]}
        new_by_identity: dict[str, dict[str, object]] = {}
        for row in rows[table]:
            clean = {column: row.get(column) for column in columns}
            key = _identity(clean, keys, table, column_types)
            if key in old:
                if _semantic_row(table, old[key], column_types) != _semantic_row(table, clean, column_types):
                    raise ValueError(f"conflicting existing append-only row in {table}")
                continue
            if key in new_by_identity and _semantic_row(table, new_by_identity[key], column_types) != _semantic_row(table, clean, column_types):
                raise ValueError(f"conflicting duplicate source row in {table}")
            new_by_identity[key] = clean
        planned[table] = list(new_by_identity.values())
    return planned


def _insert_plan(conn: Any, plan: dict[str, list[dict[str, object]]], batch_id: str,
                 manifest: dict[str, object],
                 column_types: dict[tuple[str, str], str] | None = None) -> dict[str, int]:
    inserted: dict[str, int] = {}
    for table, new_rows in plan.items():
        columns, keys = TABLES[table]
        placeholders = ",".join("%s" for _ in columns)
        query = (f'INSERT INTO "{table}" ({", ".join(columns)}) VALUES ({placeholders}) '
                 "ON CONFLICT DO NOTHING")
        inserted[table] = 0
        for row in new_rows:
            with conn.cursor() as cursor:
                bound = tuple(_bind_value(table, column, row[column], column_types)
                              if column_types is not None else row[column] for column in columns)
                cursor.execute(query, bound)
                if cursor.rowcount == 1:
                    identity_json = _identity(row, keys)
                    current = _select_identity_row(conn, table, columns, keys,
                                                   {key: row[key] for key in keys}, column_types)
                    if current is None:
                        raise ValueError("inserted append-only row could not be read back")
                    row_hash = _semantic_row_hash(table, current, column_types)
                    cursor.execute(
                        "INSERT INTO production_data_import_batch_rows "
                        "(batch_id,table_name,row_identity_json,row_sha256) VALUES (%s,%s,%s,%s)",
                        (batch_id, table, identity_json, row_hash),
                    )
                    inserted[table] += 1
                else:
                    identity = {key: row[key] for key in keys}
                    current = _select_identity_row(conn, table, columns, keys, identity, column_types)
                    if current is None or _semantic_row(table, current, column_types) != _semantic_row(table, row, column_types):
                        raise ValueError(f"concurrent append-only conflict in {table}")
    with conn.cursor() as cursor:
        cursor.execute(
            "INSERT INTO production_data_import_batches(batch_id,source_manifest_json,imported_at,state) "
            "VALUES (%s,%s,%s,%s)",
            (batch_id, _canonical(manifest), datetime.now(timezone.utc).isoformat(), "committed"),
        )
    return inserted


def rollback_import(*, database_url: str, target: str, batch_id: str,
                    backup_path: Path, repo_root: Path) -> dict[str, object]:
    """Remove only journaled rows from one batch, refusing shared row ownership."""
    if target != EXPECTED_TARGET or not _is_digest(batch_id):
        raise ValueError("rollback requires the exact xbray-api batch ID")
    if os.environ.get(IMPORT_TARGET_ENV) != "xbray-api-production":
        raise ValueError("set XBRAY_IMPORT_TARGET=xbray-api-production in the authorized runtime")
    if not database_url.strip() or not urlparse(database_url).scheme.startswith("postgres"):
        raise ValueError("DATABASE_URL must identify PostgreSQL; value is never logged")
    import psycopg
    from psycopg.rows import dict_row

    connect: Any = psycopg.connect
    conn = connect(database_url, prepare_threshold=None, row_factory=dict_row)
    try:
        with conn.transaction():
            with conn.cursor() as cursor:
                cursor.execute("SELECT pg_advisory_xact_lock(%s)", (0x5842524159494D50,))
            _check_migrations(conn)
            column_types = _load_column_types(conn)
            with conn.cursor() as cursor:
                cursor.execute("SELECT state FROM production_data_import_batches WHERE batch_id=%s",
                               (batch_id,))
                state = _first_value(cursor.fetchone())
                if state != "committed":
                    raise ValueError("batch is missing or already rolled back")
                cursor.execute(
                    "SELECT table_name,row_identity_json,row_sha256 FROM production_data_import_batch_rows "
                    "WHERE batch_id=%s", (batch_id,),
                )
                journal = cursor.fetchall()
                cursor.execute(
                    "SELECT table_name,row_identity_json FROM production_data_import_batch_rows "
                    "WHERE batch_id<>%s", (batch_id,),
                )
                other_owners = {
                    (str(_row_values(row, "table_name", "row_identity_json")[0]),
                     str(_row_values(row, "table_name", "row_identity_json")[1]))
                    for row in cursor.fetchall()
                }
            owned = [(str(table), str(identity), str(row_hash))
                     for table, identity, row_hash in (_journal_values(row) for row in journal)]
            if not owned or any(item in other_owners for item in owned):
                raise ValueError("batch has no uniquely owned rows or is shared by another import")
            backup_sha = _backup_tables(conn, backup_path, repo_root)
            removed = 0
            for table, identity_json, expected_row_hash in owned:
                if table not in TABLES:
                    raise ValueError("journal contains an unsupported table")
                _columns, keys = TABLES[table]
                identity = json.loads(identity_json)
                if not isinstance(identity, dict):
                    raise TypeError("invalid batch row identity")
                identity = cast(dict[str, object], identity)
                if set(identity) != set(keys):
                    raise ValueError("invalid batch row identity")
                current = _select_identity_row(conn, table, _columns, keys, identity, column_types)
                if current is None or _semantic_row_hash(table, current, column_types) != expected_row_hash:
                    raise ValueError("batch row changed since import; rollback refused")
                where = " AND ".join(f'"{key}"=%s' for key in keys)
                query = f'DELETE FROM "{table}" WHERE {where}'
                with conn.cursor() as cursor:
                    params = tuple(_bind_value(table, key, identity[key], column_types) for key in keys)
                    cursor.execute(query, params)
                    if cursor.rowcount != 1:
                        raise ValueError("batch row no longer matches its recorded identity")
                    cursor.execute(
                        "DELETE FROM production_data_import_batch_rows "
                        "WHERE batch_id=%s AND table_name=%s AND row_identity_json=%s",
                        (batch_id, table, identity_json),
                    )
                removed += 1
            with conn.cursor() as cursor:
                cursor.execute("UPDATE production_data_import_batches SET state=%s WHERE batch_id=%s",
                               ("rolled_back", batch_id))
            return {"target": EXPECTED_TARGET, "batch_id": batch_id, "removed_rows": removed,
                    "backup_sha256": backup_sha, "state": "rolled_back"}
    finally:
        conn.close()


def run_import(*, di_db: Path, non_di_db: Path, focus_db: Path, database_url: str,
               target: str, execute: bool = False, expected_sha256: str | None = None,
               expected_total_rows: int | None = None, backup_path: Path | None = None,
               repo_root: Path) -> dict[str, object]:
    if target != EXPECTED_TARGET:
        raise ValueError("target must be the confirmed xbray-api production project")
    if not database_url.strip() or not urlparse(database_url).scheme.startswith("postgres"):
        raise ValueError("DATABASE_URL must identify PostgreSQL; value is never logged")
    now = datetime.now(timezone.utc)
    rows, manifest = _source_bundle(di_db, non_di_db, focus_db)
    validation = _validate_bundle(rows, now)
    if execute:
        if os.environ.get(IMPORT_TARGET_ENV) != "xbray-api-production":
            raise ValueError("set XBRAY_IMPORT_TARGET=xbray-api-production in the authorized runtime")
        if expected_sha256 != manifest["bundle_sha256"] or expected_total_rows != manifest["total_rows"]:
            raise ValueError("execute requires the exact dry-run bundle hash and row count")
        if backup_path is None:
            raise ValueError("execute requires an external pre-import backup path")
    import psycopg
    from psycopg.rows import dict_row

    connect: Any = psycopg.connect
    conn = connect(database_url, prepare_threshold=None, row_factory=dict_row)
    try:
        with conn.transaction():
            if execute:
                with conn.cursor() as cursor:
                    cursor.execute("SELECT pg_advisory_xact_lock(%s)", (0x5842524159494D50,))
            else:
                with conn.cursor() as cursor:
                    cursor.execute("SET TRANSACTION READ ONLY")
            _check_migrations(conn)
            column_types = _load_column_types(conn)
            existing = {table: _table_rows(conn, table, columns)
                        for table, (columns, _keys) in TABLES.items()}
            plan = _plan(rows, existing, column_types)
            current: dict[str, int] = {}
            for table in TABLES:
                with conn.cursor() as cursor:
                    cursor.execute(f'SELECT COUNT(*) FROM "{table}"')
                    count_value = _first_value(cursor.fetchone())
                    if not isinstance(count_value, int):
                        raise TypeError("missing PostgreSQL count result")
                    current[table] = count_value
            already = False
            with conn.cursor() as cursor:
                cursor.execute("SELECT state FROM production_data_import_batches WHERE batch_id=%s",
                               (manifest["bundle_sha256"],))
                record = cursor.fetchone()
                state = _first_value(record)
                already = record is not None
                if record is not None and state != "committed":
                    raise ValueError("bundle batch exists but is not committed")
            backup_sha = None
            if execute and not already:
                assert backup_path is not None
                backup_sha = _backup_tables(conn, backup_path, repo_root)
                counts = _insert_plan(conn, plan, str(manifest["bundle_sha256"]), manifest,
                                      column_types)
            else:
                counts = {table: 0 if already else len(values) for table, values in plan.items()}
            return {
                "dry_run": not execute, "target": EXPECTED_TARGET,
                "bundle_sha256": manifest["bundle_sha256"], "total_rows": manifest["total_rows"],
                "source_files_sha256": manifest["source_files_sha256"],
                "source_row_counts": manifest["row_counts"], "planned_inserts": counts,
                "already_imported": already, "production_counts_before": current,
                "validation": validation, "backup_sha256": backup_sha,
            }
    finally:
        conn.close()
