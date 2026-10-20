from __future__ import annotations

import gzip
import hashlib
import json
import os
import sys
import time
from contextlib import nullcontext
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal
from pathlib import Path
from types import ModuleType
from typing import cast
from urllib.parse import urlparse
from uuid import UUID

import pytest

from macro_score import production_import as importer

NOW = datetime(2026, 10, 20, 17, tzinfo=timezone.utc)
STAMP = "2026-10-20T15:35:20.039566+00:00"
DIGEST = "a" * 64


def _hash(value: object) -> str:
    encoded = json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
    return hashlib.sha256(encoded.encode()).hexdigest()


def _macro_row(component: str, month: str, formula: str, value: float = 1.0) -> dict[str, object]:
    inputs = {"ref": month}
    return {
        "component_key": component, "reference_month": month, "value": value, "unit": "pct",
        "formula_version": formula, "source_basis": "retrospective_reference", "fetched_at": STAMP,
        "inputs_json": json.dumps(inputs, sort_keys=True, separators=(",", ":")),
        "inputs_hash": _hash(inputs), "reason": None,
    }


def _bundle() -> dict[str, list[dict[str, object]]]:
    macros: list[dict[str, object]] = []
    non_di = sorted(importer.NON_DI_COMPONENTS)
    months = [f"2024-{month:02d}" for month in range(1, 13)] + [
        f"2025-{month:02d}" for month in range(1, 13)
    ]
    for component in non_di:
        macros.extend(_macro_row(component, month, "macro-retrospective-1.0.0") for month in months)
    di_components = ("juro_real_ex_ante_1y", "inclinacao_di_5y_1y")
    for component in di_components:
        macros.extend(_macro_row(component, month, "macro-retrospective-di-2.0.0") for month in months)
    return {
        "retrospective_di_observations": [{
            "trade_date": "2024-01-31", "symbol": "DI1F25", "maturity": "2025-01-02",
            "rate": 0.1, "source_hash": DIGEST, "fetched_at": STAMP, "revision": f"sha256:{DIGEST}",
            "source_basis": "retrospective_reference", "source_name": "SPRD240131.zip",
            "source_member": "member.xml", "maturity_basis": "legacy_local_unverified", "security_id": "BRDI1F",
        }],
        "b3_di_instrument_snapshots": [{
            "snapshot_date": "2025-08-29", "report_date": "2025-08-29", "symbol": "DI1F25",
            "security_id": "BRDI1F", "maturity": "2025-01-02", "trading_start": None,
            "trading_end": "2024-12-30", "fin_instrm_id": "ID-1", "source_hash": DIGEST,
            "source_name": "catalog.xml", "message_definition": "bvmf.100.02", "message_id": "id",
            "captured_at": STAMP, "available_at": STAMP, "source_basis": "b3_instrument_snapshot_v1",
        }],
        "retrospective_di_price_identities": [{
            "trade_date": "2024-01-31", "symbol": "DI1F25", "source_hash": DIGEST,
            "source_member": "member.xml", "security_id": "ID-1", "captured_at": STAMP,
            "source_basis": "b3_price_identity_v1",
        }],
        "retrospective_macro_values": macros,
        "observation_vintages": [{
            "provider": "focus", "series_key": importer.FOCUS_KEY, "reference_period": "2024-01-31",
            "dimensions_json": "{}", "dimensions_hash": _hash({}), "raw_value_json": "{\"value\":3.0}",
            "source_published_at": None, "first_seen_at": STAMP, "available_at": STAMP,
            "payload_hash": _hash({"value": 3.0}), "revision_id": _hash({"value": 3.0})[:16],
        }],
    }


def _postgres_types(numeric_overrides: set[tuple[str, str]] | None = None) -> dict[tuple[str, str], str]:
    overrides = numeric_overrides or set()
    result = {
        (table, column): "text"
        for table, (columns, _keys) in importer.TABLES.items()
        for column in columns
    }
    for table, columns in importer.FLOAT_COLUMNS.items():
        for column in columns:
            result[(table, column)] = "numeric" if (table, column) in overrides else "real"
    return result


def test_bundle_validation_accepts_official_historical_rows() -> None:
    report = importer._validate_bundle(_bundle(), NOW)
    assert report["point_in_time"] is False
    assert report["di_distinct_months"] == {
        "inclinacao_di_5y_1y": 24, "juro_real_ex_ante_1y": 24,
    }
    assert len(report["coverage_distinct_months"]) == 18


@pytest.mark.parametrize("mutate", ["bad_hash", "future", "wrong_basis", "nan", "wrong_formula"])
def test_bundle_validation_fails_closed(mutate: str) -> None:
    rows = _bundle()
    if mutate == "bad_hash":
        rows["retrospective_macro_values"][0]["inputs_hash"] = DIGEST
    elif mutate == "future":
        rows["retrospective_di_observations"][0]["fetched_at"] = "2027-01-01T00:00:00+00:00"
    elif mutate == "wrong_basis":
        rows["b3_di_instrument_snapshots"][0]["source_basis"] = "calendar_guess"
    elif mutate == "nan":
        rows["retrospective_macro_values"][0]["value"] = float("nan")
    else:
        rows["retrospective_macro_values"][0]["formula_version"] = "fixture"
    with pytest.raises(ValueError):
        importer._validate_bundle(rows, NOW)


def test_bundle_validation_rejects_short_history_and_registry_conflicts() -> None:
    rows = _bundle()
    rows["retrospective_macro_values"] = [
        row for row in rows["retrospective_macro_values"]
        if row["component_key"] != "juro_real_ex_ante_1y" or row["reference_month"] != "2024-01"
    ]
    with pytest.raises(ValueError, match="24 months"):
        importer._validate_bundle(rows, NOW)
    rows = _bundle()
    conflict = dict(rows["b3_di_instrument_snapshots"][0], maturity="2025-02-01")
    conflict["source_hash"] = "b" * 64
    rows["b3_di_instrument_snapshots"].append(conflict)
    with pytest.raises(ValueError, match="conflicting"):
        importer._validate_bundle(rows, NOW)


@pytest.mark.parametrize("table,mutation", [
    ("retrospective_di_observations", "source_basis"),
    ("retrospective_di_observations", "rate"),
    ("retrospective_di_price_identities", "source_basis"),
    ("observation_vintages", "provider"),
    ("observation_vintages", "payload_hash"),
])
def test_bundle_validation_rejects_tampered_source_tables(table: str, mutation: str) -> None:
    rows = _bundle()
    row = rows[table][0]
    if mutation == "rate":
        row[mutation] = float("inf")
    elif mutation == "payload_hash":
        row[mutation] = "z" * 64
    else:
        row[mutation] = "invalid"
    with pytest.raises((ValueError, TypeError)):
        importer._validate_bundle(rows, NOW)


def test_timestamp_and_digest_helpers_reject_bad_inputs() -> None:
    assert importer._is_digest(DIGEST)
    assert not importer._is_digest("not-a-digest")
    assert not importer._is_digest("z" * 64)
    with pytest.raises(TypeError):
        importer._aware_timestamp(None, field="captured_at", now=NOW)
    with pytest.raises(ValueError, match="invalid captured_at"):
        importer._aware_timestamp("not-an-iso-date", field="captured_at", now=NOW)
    with pytest.raises(ValueError, match="timezone"):
        importer._aware_timestamp("2026-10-20T12:00:00", field="captured_at", now=NOW)
    with pytest.raises(ValueError, match="future"):
        importer._aware_timestamp("2027-01-01T00:00:00Z", field="captured_at", now=NOW)


def test_macro_validation_rejects_basis_type_component_json_and_short_coverage() -> None:
    rows = _bundle()["retrospective_macro_values"]
    original = rows[0]
    for field, value, error in (
        ("source_basis", "production", "non-retrospective"),
        ("component_key", None, "macro key"),
        ("component_key", "unknown", "unexpected retrospective component"),
        ("inputs_hash", "bad", "invalid retrospective macro provenance"),
        ("inputs_json", "NaN", "invalid retrospective input payload"),
        ("value", True, "non-finite retrospective value"),
    ):
        row = dict(original)
        row[field] = value
        with pytest.raises((ValueError, TypeError), match=error):
            importer._validate_macro_rows([row], NOW)
    absent = [row for row in rows if row["component_key"] != next(iter(importer.NON_DI_COMPONENTS))]
    with pytest.raises(ValueError, match="does not cover"):
        importer._validate_macro_rows(absent, NOW)
    short = [row for row in rows if not (
        row["component_key"] == next(iter(importer.NON_DI_COMPONENTS)) and row["reference_month"] == "2024-01"
    )]
    with pytest.raises(ValueError, match="24-month"):
        importer._validate_macro_rows(short, NOW)


def test_vintage_json_and_result_row_helpers_reject_invalid_shapes() -> None:
    vintage = _bundle()["observation_vintages"][0]
    for mutation in ("missing_json", "bad_hash"):
        row = dict(vintage)
        if mutation == "missing_json":
            row.pop("raw_value_json")
        else:
            row["dimensions_hash"] = "0" * 64
        with pytest.raises(ValueError):
            importer._validate_vintages([row], NOW)
    assert importer._first_value({"v": 1}) == 1
    assert importer._first_value(None) is None
    assert importer._row_values({"a": 1, "b": 2}, "a", "b") == (1, 2)
    assert importer._journal_values({"table_name": "t", "row_identity_json": "{}", "row_sha256": DIGEST}) == (
        "t", "{}", DIGEST,
    )
    with pytest.raises(ValueError):
        importer._first_value(1)
    with pytest.raises(ValueError):
        importer._row_values((1,), "a", "b")


def test_source_reader_is_read_only_and_hashes_file(tmp_path: Path) -> None:
    import sqlite3

    source = tmp_path / "source.db"
    connection = sqlite3.connect(source)
    try:
        connection.execute("CREATE TABLE t (id INTEGER, value TEXT)")
        connection.execute("INSERT INTO t VALUES (1, 'official')")
        connection.commit()
    finally:
        connection.close()
    before = importer._file_hash(source)
    assert importer._read_source(source, "t") == [{"id": 1, "value": "official"}]
    assert importer._file_hash(source) == before
    assert importer._read_source(source, "missing") == []
    with pytest.raises(ValueError):
        importer._read_source(tmp_path / "missing.db", "t")
    with pytest.raises(ValueError):
        importer._read_source(Path("relative.db"), "t")
    with pytest.raises(ValueError, match="could not read"):
        importer._read_source(source, "t", "WHERE no_such_column = 1")


def test_source_bundle_manifest_uses_readonly_sources(monkeypatch: pytest.MonkeyPatch,
                                                      tmp_path: Path) -> None:
    import sqlite3

    rows = _bundle()
    paths = [tmp_path / name for name in ("di.db", "non-di.db", "focus.db")]
    table_sets = [
        {"retrospective_di_observations": rows["retrospective_di_observations"],
         "b3_di_instrument_snapshots": rows["b3_di_instrument_snapshots"],
         "retrospective_di_price_identities": rows["retrospective_di_price_identities"],
         "retrospective_macro_values": [row for row in rows["retrospective_macro_values"]
                                         if row["component_key"] in {"juro_real_ex_ante_1y", "inclinacao_di_5y_1y"}]},
        {"retrospective_macro_values": [row for row in rows["retrospective_macro_values"]
                                         if row["component_key"] not in {"juro_real_ex_ante_1y", "inclinacao_di_5y_1y"}]},
        {"observation_vintages": rows["observation_vintages"]},
    ]
    for path, tables in zip(paths, table_sets, strict=True):
        connection = sqlite3.connect(path)
        try:
            for table, contents in tables.items():
                columns = tuple(contents[0])
                connection.execute(f'CREATE TABLE "{table}" ({", ".join(f"{name} TEXT" for name in columns)})')
                connection.executemany(
                    f'INSERT INTO "{table}" VALUES ({",".join("?" for _ in columns)})',
                    [tuple(json.dumps(row[column]) if isinstance(row[column], (dict, list)) else row[column]
                           for column in columns) for row in contents],
                )
            connection.commit()
        finally:
            connection.close()
    retrospective = ModuleType("macro_score.retrospective")
    retrospective.reconstruct_di_ledger = lambda *_args, **_kwargs: {
        "eligible_distinct_months": {"juro_real_ex_ante_1y": 24, "inclinacao_di_5y_1y": 24}
    }
    monkeypatch.setitem(sys.modules, "macro_score.retrospective", retrospective)
    result, manifest = importer._source_bundle(*paths)
    assert manifest["total_rows"] == sum(manifest["row_counts"].values())
    assert manifest["bundle_sha256"] == _hash({
        "manifest": {key: value for key, value in manifest.items() if key != "bundle_sha256"},
        "rows": {key: sorted(value, key=importer._canonical) for key, value in result.items()},
    })
    assert manifest["point_in_time"] is False
    assert set(manifest["source_files_sha256"]) == {"di_ledger", "non_di_macro_ledger", "focus_vintages"}


def test_source_bundle_rejects_unverified_di_threshold(monkeypatch: pytest.MonkeyPatch,
                                                       tmp_path: Path) -> None:
    from macro_score import retrospective

    paths = [tmp_path / f"{name}.db" for name in ("di", "non-di", "focus")]
    import sqlite3
    for path in paths:
        connection = sqlite3.connect(path)
        connection.close()
    monkeypatch.setattr(retrospective, "reconstruct_di_ledger", lambda *_args, **_kwargs: {
        "eligible_distinct_months": {"juro_real_ex_ante_1y": 0, "inclinacao_di_5y_1y": 31}
    })
    with pytest.raises(ValueError, match="24-month gate"):
        importer._source_bundle(*paths)


def test_plan_is_idempotent_and_refuses_conflicting_existing_row() -> None:
    rows = _bundle()
    table = "retrospective_macro_values"
    columns, _keys = importer.TABLES[table]
    first = {column: rows[table][0].get(column) for column in columns}
    existing = {name: [] for name in importer.TABLES}
    existing[table] = [first]
    plan = importer._plan(rows, existing)
    assert len(plan[table]) == len(rows[table]) - 1
    conflict = dict(first, value=99.0)
    existing[table] = [conflict]
    with pytest.raises(ValueError, match="conflicting existing"):
        importer._plan(rows, existing)


def test_plan_semantically_normalizes_native_dates_timestamps_and_json() -> None:
    source = _bundle()
    old = {table: [] for table in importer.TABLES}
    di_table = "retrospective_di_observations"
    source_di = source[di_table][0]
    old[di_table] = [dict(
        source_di,
        trade_date=date.fromisoformat(str(source_di["trade_date"])),
        maturity=date.fromisoformat(str(source_di["maturity"])),
        fetched_at=datetime.fromisoformat(str(source_di["fetched_at"])),
        rate=Decimal("0.10"),
    )]
    macro_table = "retrospective_macro_values"
    source_macro = source[macro_table][0]
    parsed = json.loads(str(source_macro["inputs_json"]))
    old[macro_table] = [dict(
        source_macro,
        fetched_at=datetime.fromisoformat(str(source_macro["fetched_at"])),
        value=Decimal("1.0"),
        inputs_json=json.dumps(parsed, indent=3),
    )]
    plan = importer._plan(source, old)
    assert plan[di_table] == []
    assert plan[macro_table] == source[macro_table][1:]
    assert importer._semantic_timestamp(
        datetime.fromisoformat("2026-10-20T17:00:00+03:00"), "t", "ts"
    ) == importer._semantic_timestamp("2026-10-20T14:00:00Z", "t", "ts")
    assert importer._semantic_float(Decimal("1.25"), "t", "v", "numeric") == importer._semantic_float(1.25, "t", "v", "numeric")
    assert importer._semantic_float(Decimal("0.10"), "t", "v", "numeric") == importer._semantic_float(0.1, "t", "v", "numeric")
    assert importer._semantic_float(Decimal("-1.23456789"), "t", "v", "numeric") == importer._semantic_float(-1.23456789, "t", "v", "numeric")
    assert importer._semantic_float(Decimal("1.23456789E+12"), "t", "v", "numeric") == importer._semantic_float(1.23456789e12, "t", "v", "numeric")
    assert importer._semantic_float(Decimal("12345678901234567890.123456789"), "t", "v", "numeric")["numeric"] == "12345678901234567890.123456789"
    assert importer._semantic_float(Decimal("0.10000000000000000001"), "t", "v", "numeric") != importer._semantic_float(0.1, "t", "v", "numeric")
    assert importer._semantic_float(Decimal("0.1"), "t", "v", "double precision") == importer._semantic_float(0.1, "t", "v", "double precision")
    assert importer._semantic_float(Decimal("0.1"), "t", "v", "real") == importer._semantic_float(0.1, "t", "v", "real")
    numeric_types = {("retrospective_macro_values", "value"): "numeric"}
    assert importer._bind_value("retrospective_macro_values", "value", 0.1, numeric_types) == Decimal("0.1")
    assert importer._bind_value("retrospective_macro_values", "value", Decimal("0.10000000000000000001"), numeric_types) == Decimal("0.10000000000000000001")


def test_semantic_normalization_is_strict_for_date_timezone_and_types() -> None:
    assert importer._semantic_date("2026-10-20", "t", "d") == "2026-10-20"
    assert importer._semantic_date(date(2026, 10, 20), "t", "d") == "2026-10-20"
    assert importer._semantic_timestamp("2026-10-20T17:00:00+03:00", "t", "ts") == \
        "2026-10-20T14:00:00.000000+00:00"
    assert importer._semantic_value("retrospective_macro_values", "reference_month", "2026-09") == "2026-09"
    with pytest.raises(ValueError):
        importer._semantic_date("2026-2-1", "t", "d")
    with pytest.raises(TypeError):
        importer._semantic_date(datetime(2026, 10, 20), "t", "d")
    with pytest.raises(ValueError, match="timezone"):
        importer._semantic_timestamp("2026-10-20T17:00:00", "t", "ts")
    with pytest.raises(TypeError, match="schema type"):
        importer._semantic_value("retrospective_macro_values", "unit", Decimal("2"))


def test_destination_float_semantics_and_explicit_binding_rules() -> None:
    real = importer._semantic_float(1.23456789, "t", "v", "real")
    rounded_source = importer._semantic_float(1.2345678806304932, "t", "v", "real")
    assert real == rounded_source
    assert importer._semantic_float(1.23456789, "t", "v", "double precision") != \
        importer._semantic_float(1.2345678806304932, "t", "v", "double precision")
    assert importer._semantic_float(Decimal("1E+4"), "t", "v", "numeric") == \
        importer._semantic_float(Decimal("10000.000"), "t", "v", "numeric")
    with pytest.raises(ValueError):
        importer._semantic_float(Decimal("NaN"), "t", "v", "numeric")
    with pytest.raises(TypeError):
        importer._semantic_float("0.1", "t", "v", "numeric")
    with pytest.raises(TypeError):
        importer._semantic_float(True, "t", "v", "real")
    with pytest.raises(ValueError):
        importer._semantic_float(float("inf"), "t", "v", "double precision")
    with pytest.raises(OverflowError):
        importer._semantic_float(1e100, "t", "v", "real")
    float_types = {("retrospective_macro_values", "value"): "real"}
    assert importer._bind_value("retrospective_macro_values", "value", 0.1, float_types) == 0.1
    assert importer._bind_value("retrospective_macro_values", "value", None, float_types) is None
    date_types = {("retrospective_di_observations", "trade_date"): "date"}
    assert importer._bind_value("retrospective_di_observations", "trade_date", "2024-01-31", date_types) == date(2024, 1, 31)
    timestamp_types = {("retrospective_di_observations", "fetched_at"): "timestamp with time zone"}
    bound_time = importer._bind_value("retrospective_di_observations", "fetched_at", STAMP, timestamp_types)
    assert isinstance(bound_time, datetime) and bound_time.utcoffset() == timedelta(0)
    with pytest.raises(ValueError, match="timezone"):
        importer._bind_value("retrospective_di_observations", "fetched_at", "2026-10-20T12:00:00", timestamp_types)
    json_types = {("retrospective_macro_values", "inputs_json"): "text"}
    assert importer._bind_value("retrospective_macro_values", "inputs_json", '{"b":2,"a":1}', json_types) == '{"a":1,"b":2}'
    with pytest.raises(TypeError, match="JSON source"):
        importer._bind_value("retrospective_macro_values", "inputs_json", {}, json_types)


def test_column_type_resolution_requires_complete_supported_schema() -> None:
    types = _postgres_types({("retrospective_di_observations", "rate")})

    class MetadataCursor(_Cursor):
        def execute(self, query: str, params: object = ()) -> None:
            self.description = [("table_name",), ("column_name",), ("data_type",)]
            self.rows = [(table, column, pg_type) for (table, column), pg_type in types.items()]

    class MetadataConnection(_Connection):
        def cursor(self) -> MetadataCursor:
            return MetadataCursor(self)

    loaded = importer._load_column_types(MetadataConnection())
    assert loaded == types
    types.pop(("observation_vintages", "revision_id"))
    with pytest.raises(ValueError, match="missing required"):
        importer._load_column_types(MetadataConnection())
    types[("observation_vintages", "revision_id")] = "jsonb"
    with pytest.raises(ValueError, match="unsupported target schema"):
        importer._load_column_types(MetadataConnection())


def test_semantic_json_rejects_non_text_and_malformed_payload() -> None:
    with pytest.raises(TypeError, match="JSON storage"):
        importer._semantic_value("retrospective_macro_values", "inputs_json", {"a": 1})
    with pytest.raises(ValueError, match="JSON"):
        importer._semantic_value("retrospective_macro_values", "inputs_json", "NaN")


class _Cursor:
    def __init__(self, connection: "_Connection") -> None:
        self.connection = connection
        self.description: list[tuple[str]] = []
        self.rows: list[object] = []
        self.rowcount = 0

    def __enter__(self) -> "_Cursor":
        return self

    def __exit__(self, *_args: object) -> None:
        return None

    def execute(self, query: str, params: object = ()) -> None:
        self.connection.queries.append(query)
        if query.startswith("SELECT version FROM schema_migrations"):
            self.rows = [(name,) for name in importer.REQUIRED_MIGRATIONS]
        elif query.startswith("SELECT COUNT(*)"):
            self.rows = [(0,)]
        elif query.startswith("SELECT state FROM production_data_import_batches"):
            self.rows = []
        elif query.startswith("SELECT") and "FROM \"" in query:
            names = query.split(" FROM ", 1)[0].removeprefix("SELECT ").split(", ")
            self.description = [(name,) for name in names]
            self.rows = []
        else:
            self.rows = []
        self.rowcount = 1 if query.startswith("INSERT") else 0

    def fetchone(self) -> object | None:
        return self.rows.pop(0) if self.rows else None

    def fetchall(self) -> list[object]:
        return self.rows


class _Connection:
    def __init__(self) -> None:
        self.queries: list[str] = []
        self.closed = False

    def cursor(self) -> _Cursor:
        return _Cursor(self)

    def transaction(self):
        return nullcontext()

    def close(self) -> None:
        self.closed = True


def _fake_psycopg(monkeypatch: pytest.MonkeyPatch, connection: _Connection) -> None:
    psycopg = ModuleType("psycopg")
    psycopg.connect = lambda *_args, **_kwargs: connection  # type: ignore[attr-defined]
    rows_module = ModuleType("psycopg.rows")
    rows_module.dict_row = object()  # type: ignore[attr-defined]
    monkeypatch.setitem(sys.modules, "psycopg", psycopg)
    monkeypatch.setitem(sys.modules, "psycopg.rows", rows_module)


def test_dry_run_is_read_only_and_reports_plan(monkeypatch: pytest.MonkeyPatch) -> None:
    conn = _Connection()
    _fake_psycopg(monkeypatch, conn)
    rows = _bundle()
    manifest = {"bundle_sha256": DIGEST, "total_rows": 1, "row_counts": {"t": 1},
                "source_files_sha256": {}}
    monkeypatch.setattr(importer, "_source_bundle", lambda *_args: (rows, manifest))
    monkeypatch.setattr(importer, "_validate_bundle", lambda *_args: {"point_in_time": False})
    monkeypatch.setattr(importer, "_load_column_types", lambda *_args: _postgres_types())
    result = importer.run_import(
        di_db=Path("/di.db"), non_di_db=Path("/non.db"), focus_db=Path("/focus.db"),
        database_url="postgresql://redacted", target="xbray-api", repo_root=Path("/repo"),
    )
    assert result["dry_run"] is True
    assert "SET TRANSACTION READ ONLY" in conn.queries
    assert not any(query.startswith("INSERT") for query in conn.queries)
    assert conn.closed


def test_execute_preflight_requires_target_hash_and_backup(monkeypatch: pytest.MonkeyPatch,
                                                           tmp_path: Path) -> None:
    rows = _bundle()
    manifest = {"bundle_sha256": DIGEST, "total_rows": 1, "row_counts": {"t": 1},
                "source_files_sha256": {}}
    monkeypatch.setattr(importer, "_source_bundle", lambda *_args: (rows, manifest))
    monkeypatch.setattr(importer, "_validate_bundle", lambda *_args: {"point_in_time": False})
    monkeypatch.setattr(importer, "_load_column_types", lambda *_args: _postgres_types())
    arguments = dict(
        di_db=Path("/di.db"), non_di_db=Path("/non.db"), focus_db=Path("/focus.db"),
        database_url="postgresql://redacted", target="xbray-api", execute=True,
        expected_sha256=DIGEST, expected_total_rows=1, backup_path=tmp_path / "backup.gz",
        repo_root=Path("/repo"),
    )
    monkeypatch.delenv(importer.IMPORT_TARGET_ENV, raising=False)
    with pytest.raises(ValueError, match="authorized runtime"):
        importer.run_import(**arguments)
    monkeypatch.setenv(importer.IMPORT_TARGET_ENV, "xbray-api-production")
    with pytest.raises(ValueError, match="exact dry-run"):
        importer.run_import(**{**arguments, "expected_sha256": "b" * 64})
    with pytest.raises(ValueError, match="PostgreSQL"):
        importer.run_import(**{**arguments, "database_url": "sqlite:///unsafe.db"})


class _RollbackCursor(_Cursor):
    def execute(self, query: str, params: object = ()) -> None:
        self.connection.queries.append(query)
        if query.startswith("SELECT version FROM schema_migrations"):
            self.rows = [(name,) for name in importer.REQUIRED_MIGRATIONS]
        elif query.startswith("SELECT state FROM production_data_import_batches"):
            self.rows = [("committed",)]
        elif query.startswith("SELECT table_name,row_identity_json") and "batch_id<>" in query:
            self.rows = []
        elif query.startswith("SELECT table_name,row_identity_json"):
            key = {"component_key": "activity", "reference_month": "2024-01",
                   "formula_version": "macro-retrospective-1.0.0", "inputs_hash": DIGEST}
            current = {"component_key": "activity", "reference_month": "2024-01", "value": 1.0,
                       "unit": "pct", "formula_version": "macro-retrospective-1.0.0",
                       "source_basis": "retrospective_reference", "fetched_at": STAMP,
                       "inputs_json": '{"ref":"2024-01"}', "inputs_hash": _hash({"ref": "2024-01"}),
                       "reason": None}
            row_hash = importer._semantic_row_hash("retrospective_macro_values", current, _postgres_types())
            self.rows = [("retrospective_macro_values", json.dumps(key, sort_keys=True, separators=(",", ":")), row_hash)]
        elif query.startswith("SELECT COUNT(*)"):
            self.rows = [(0,)]
        elif query.startswith("SELECT") and "FROM \"" in query:
            names = query.split(" FROM ", 1)[0].removeprefix("SELECT ").split(", ")
            self.description = [(name,) for name in names]
            self.rows = []
        else:
            self.rows = []
        self.rowcount = 1 if query.startswith(("INSERT", "DELETE", "UPDATE")) else 0


class _RollbackConnection(_Connection):
    def cursor(self) -> _RollbackCursor:
        return _RollbackCursor(self)


def test_rollback_removes_only_journaled_rows(monkeypatch: pytest.MonkeyPatch,
                                               tmp_path: Path) -> None:
    conn = _RollbackConnection()
    _fake_psycopg(monkeypatch, conn)
    current = {"component_key": "activity", "reference_month": "2024-01", "value": 1.0,
               "unit": "pct", "formula_version": "macro-retrospective-1.0.0",
               "source_basis": "retrospective_reference", "fetched_at": STAMP,
               "inputs_json": '{"ref":"2024-01"}', "inputs_hash": _hash({"ref": "2024-01"}),
               "reason": None}
    monkeypatch.setattr(importer, "_select_identity_row", lambda *_args: current)
    monkeypatch.setattr(importer, "_load_column_types", lambda *_args: _postgres_types())
    monkeypatch.setenv(importer.IMPORT_TARGET_ENV, "xbray-api-production")
    backup = tmp_path / "rollback-backup.gz"
    result = importer.rollback_import(
        database_url="postgresql://redacted", target="xbray-api", batch_id=DIGEST,
        backup_path=backup, repo_root=Path("/repo"),
    )
    assert result["removed_rows"] == 1
    assert result["state"] == "rolled_back"
    assert any(query.startswith('DELETE FROM "retrospective_macro_values"') for query in conn.queries)
    assert any("UPDATE production_data_import_batches SET state" in query for query in conn.queries)
    assert conn.closed


def test_rollback_refuses_invalid_target_and_batch(monkeypatch: pytest.MonkeyPatch) -> None:
    with pytest.raises(ValueError, match="exact xbray-api"):
        importer.rollback_import(
            database_url="postgresql://redacted", target="preview", batch_id=DIGEST,
            backup_path=Path("/tmp/backup.gz"), repo_root=Path("/repo"),
        )
    with pytest.raises(ValueError, match="exact xbray-api"):
        importer.rollback_import(
            database_url="postgresql://redacted", target="xbray-api", batch_id="bad",
            backup_path=Path("/tmp/backup.gz"), repo_root=Path("/repo"),
        )


def test_execute_requires_exact_hash_marker_and_backup(monkeypatch: pytest.MonkeyPatch,
                                                       tmp_path: Path) -> None:
    conn = _Connection()
    _fake_psycopg(monkeypatch, conn)
    rows = _bundle()
    manifest = {"bundle_sha256": DIGEST, "total_rows": 1, "row_counts": {"t": 1},
                "source_files_sha256": {}}
    monkeypatch.setattr(importer, "_source_bundle", lambda *_args: (rows, manifest))
    monkeypatch.setattr(importer, "_validate_bundle", lambda *_args: {"point_in_time": False})
    monkeypatch.setattr(importer, "_load_column_types", lambda *_args: _postgres_types())
    def readback(_conn, table, columns, keys, identity, *_column_types):
        for candidate in rows[table]:
            if all(candidate[key] == identity[key] for key in keys):
                return {column: candidate.get(column) for column in columns}
        return None
    monkeypatch.setattr(importer, "_select_identity_row", readback)
    monkeypatch.setenv(importer.IMPORT_TARGET_ENV, "xbray-api-production")
    backup = tmp_path / "backup.json.gz"
    result = importer.run_import(
        di_db=Path("/di.db"), non_di_db=Path("/non.db"), focus_db=Path("/focus.db"),
        database_url="postgresql://redacted", target="xbray-api", execute=True,
        expected_sha256=DIGEST, expected_total_rows=1, backup_path=backup, repo_root=Path("/repo"),
    )
    assert result["dry_run"] is False
    assert backup.exists()
    assert stat_mode(backup) == 0o600
    with gzip.open(backup, "rt") as stream:
        assert json.load(stream)["format"] == "xbray-production-ledger-backup-v1"
    assert any("pg_advisory_xact_lock" in query for query in conn.queries)
    assert any(query.startswith("INSERT INTO production_data_import_batches") for query in conn.queries)
    assert conn.closed


def test_postgres_import_rerun_and_rollback_with_native_column_types(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path,
) -> None:
    database_url = os.environ.get("TEST_DATABASE_URL")
    if not database_url:
        pytest.skip("TEST_DATABASE_URL is not configured; real PostgreSQL integration test")
    parsed_target = urlparse(database_url)
    if parsed_target.hostname not in {"127.0.0.1", "localhost", "::1"} \
            or parsed_target.path.lstrip("/") != "xbray_test":
        pytest.skip("integration test requires the explicit local xbray_test database")
    import psycopg
    from db.migrations import run_migrations

    schema = f"import_test_{time.time_ns()}"
    admin = psycopg.connect(database_url)
    try:
        with admin.cursor() as cursor:
            cursor.execute(f'CREATE SCHEMA "{schema}"')
        admin.commit()
        scoped = psycopg.connect(database_url, options=f"-c search_path={schema}")
        try:
            with scoped.transaction():
                run_migrations(scoped, postgres=True)
            for table, fields in importer.DATE_COLUMNS.items():
                for column in fields:
                    with scoped.cursor() as cursor:
                        cursor.execute(f'ALTER TABLE "{table}" ALTER COLUMN "{column}" TYPE DATE USING NULLIF("{column}", \'\')::date')
            for table, fields in importer.TIMESTAMP_COLUMNS.items():
                for column in fields:
                    with scoped.cursor() as cursor:
                        cursor.execute(
                            f'ALTER TABLE "{table}" ALTER COLUMN "{column}" TYPE TIMESTAMPTZ '
                            f'USING NULLIF("{column}", \'\')::timestamptz'
                        )
            with scoped.cursor() as cursor:
                cursor.execute(
                    'ALTER TABLE "retrospective_di_observations" ALTER COLUMN "rate" '
                    'TYPE NUMERIC USING "rate"::numeric'
                )
                cursor.execute(
                    "SELECT data_type FROM information_schema.columns WHERE table_schema=current_schema() "
                    "AND table_name='retrospective_macro_values' AND column_name='value'"
                )
                assert cursor.fetchone()[0] == "real"
                cursor.execute(
                    "SELECT data_type FROM information_schema.columns WHERE table_schema=current_schema() "
                    "AND table_name='retrospective_di_observations' AND column_name='rate'"
                )
                assert cursor.fetchone()[0] == "numeric"
            scoped.commit()
        finally:
            scoped.close()
        original_connect = psycopg.connect

        def connect_with_schema(url: str, **kwargs: object):
            return original_connect(url, options=f"-c search_path={schema}", **kwargs)

        monkeypatch.setattr(psycopg, "connect", connect_with_schema)
        rows = _bundle()
        rows["retrospective_macro_values"][0]["value"] = 1.23456789
        manifest = {
            "manifest_version": "integration-test", "bundle_sha256": DIGEST,
            "total_rows": sum(map(len, rows.values())),
            "row_counts": {table: len(contents) for table, contents in rows.items()},
            "source_files_sha256": {},
        }
        monkeypatch.setattr(importer, "_source_bundle", lambda *_args: (rows, manifest))
        monkeypatch.setattr(importer, "_validate_bundle", lambda *_args: {"point_in_time": False})
        monkeypatch.setenv(importer.IMPORT_TARGET_ENV, "xbray-api-production")
        common = {
            "di_db": Path("/di.db"), "non_di_db": Path("/non.db"), "focus_db": Path("/focus.db"),
            "database_url": database_url, "target": "xbray-api", "expected_sha256": DIGEST,
            "expected_total_rows": manifest["total_rows"], "repo_root": Path("/repo"),
        }
        first = importer.run_import(
            **common, execute=True, backup_path=tmp_path / "pg-first-backup.json.gz",
        )
        assert sum(cast(dict[str, int], first["planned_inserts"]).values()) > 0
        inspector = original_connect(database_url, options=f"-c search_path={schema}")
        try:
            with inspector.cursor() as cursor:
                cursor.execute(
                    "SELECT value FROM retrospective_macro_values WHERE component_key=%s AND reference_month=%s",
                    (rows["retrospective_macro_values"][0]["component_key"],
                     rows["retrospective_macro_values"][0]["reference_month"]),
                )
                real_value = cursor.fetchone()[0]
                assert importer._semantic_float(real_value, "retrospective_macro_values", "value", "real") == \
                    importer._semantic_float(1.23456789, "retrospective_macro_values", "value", "real")
                cursor.execute("SELECT rate FROM retrospective_di_observations")
                assert cursor.fetchone()[0] == Decimal("0.1")
        finally:
            inspector.close()
        second = importer.run_import(
            **common, execute=True, backup_path=tmp_path / "pg-rerun-backup.json.gz",
        )
        assert second["already_imported"] is True
        assert all(value == 0 for value in cast(dict[str, int], second["planned_inserts"]).values())
        with gzip.open(tmp_path / "pg-first-backup.json.gz", "rt", encoding="utf-8") as stream:
            backup = json.load(stream)
        assert backup["format_version"] == 2
        rollback = importer.rollback_import(
            database_url=database_url, target="xbray-api", batch_id=DIGEST,
            backup_path=tmp_path / "pg-rollback-backup.json.gz", repo_root=Path("/repo"),
        )
        assert rollback["removed_rows"] > 0
        changed_batch = "b" * 64
        changed_manifest = {**manifest, "bundle_sha256": changed_batch}
        monkeypatch.setattr(importer, "_source_bundle", lambda *_args: (rows, changed_manifest))
        changed_common = {**common, "expected_sha256": changed_batch}
        importer.run_import(
            **changed_common, execute=True, backup_path=tmp_path / "pg-changed-batch-backup.json.gz",
        )
        with original_connect(database_url, options=f"-c search_path={schema}") as mutator:
            with mutator.cursor() as cursor:
                changed_row = rows["retrospective_macro_values"][0]
                cursor.execute(
                    "UPDATE retrospective_macro_values SET value=value+1 WHERE component_key=%s "
                    "AND reference_month=%s AND formula_version=%s AND inputs_hash=%s",
                    tuple(changed_row[key] for key in (
                        "component_key", "reference_month", "formula_version", "inputs_hash",
                    )),
                )
        with pytest.raises(ValueError, match="changed since import"):
            importer.rollback_import(
                database_url=database_url, target="xbray-api", batch_id=changed_batch,
                backup_path=tmp_path / "pg-mutated-rollback-backup.json.gz", repo_root=Path("/repo"),
            )
    finally:
        admin.rollback()
        with admin.cursor() as cursor:
            cursor.execute(f'DROP SCHEMA IF EXISTS "{schema}" CASCADE')
        admin.commit()
        admin.close()


def stat_mode(path: Path) -> int:
    import stat
    return stat.S_IMODE(path.stat().st_mode)


def test_import_rejects_target_and_hash_before_opening_database(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(importer, "_source_bundle", lambda *_args: pytest.fail("read should not happen"))
    with pytest.raises(ValueError, match="target"):
        importer.run_import(
            di_db=Path("/di.db"), non_di_db=Path("/non.db"), focus_db=Path("/focus.db"),
            database_url="postgresql://redacted", target="preview", repo_root=Path("/repo"),
        )


def test_backup_requires_external_new_path(tmp_path: Path) -> None:
    conn = _Connection()
    repo = tmp_path / "repo"
    repo.mkdir()
    with pytest.raises(ValueError, match="outside"):
        importer._backup_tables(conn, repo / "backup.gz", repo)


def test_backup_roundtrips_postgres_scalar_and_container_types(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path,
) -> None:
    from macro_score.production_import import _backup_decode

    row = {
        "sql_date": date(2026, 10, 20),
        "aware_timestamp": datetime(2026, 10, 20, 17, 4, 5, 123456,
                                     tzinfo=timezone(timedelta(hours=5, minutes=30)), fold=1),
        "naive_timestamp": datetime(2026, 10, 20, 17, 4, 5),
        "decimal": Decimal("12345678901234567890.0012300"),
        "nullable": None,
        "timestamp_looking_text": "2026-10-20T17:04:05+05:30",
        "bytes": b"\x00\xff",
        "memoryview": memoryview(b"pg-bytes"),
        "uuid": UUID("12345678-1234-5678-1234-567812345678"),
        "interval": timedelta(days=-2, seconds=17, microseconds=4),
        "nested": {importer.BACKUP_TYPE_KEY: "a user key", "items": [1, Decimal("1E+30")]},
    }
    table_name = "retrospective_macro_values"
    monkeypatch.setattr(
        importer, "_table_rows",
        lambda _conn, table, _columns: [row] if table == table_name else [],
    )
    path = tmp_path / "typed-backup.json.gz"
    digest = importer._backup_tables(_Connection(), path, Path("/repo"))
    assert digest
    with gzip.open(path, "rt", encoding="utf-8") as stream:
        envelope = json.load(stream)
    assert envelope["format_version"] == 2
    decoded = _backup_decode(envelope["tables"])
    assert decoded[table_name] == [row]


@pytest.mark.parametrize("value", [object(), float("nan"), float("inf"), Decimal("NaN")])
def test_backup_serializer_rejects_unsupported_or_nonfinite_values(value: object) -> None:
    with pytest.raises((TypeError, ValueError)):
        importer._backup_encode({"bad": value})


def test_unsupported_database_type_aborts_before_any_import_write(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path,
) -> None:
    conn = _Connection()
    _fake_psycopg(monkeypatch, conn)
    rows = _bundle()
    manifest = {"bundle_sha256": DIGEST, "total_rows": 1, "row_counts": {"t": 1},
                "source_files_sha256": {}}
    monkeypatch.setattr(importer, "_source_bundle", lambda *_args: (rows, manifest))
    monkeypatch.setattr(importer, "_validate_bundle", lambda *_args: {"point_in_time": False})
    monkeypatch.setenv(importer.IMPORT_TARGET_ENV, "xbray-api-production")
    table_name = "retrospective_di_observations"
    columns, _keys = importer.TABLES[table_name]
    bad_row = {column: None for column in columns}
    bad_row[columns[0]] = object()
    calls = 0

    def table_rows(_conn, table, _columns):
        nonlocal calls
        calls += 1
        if calls <= len(importer.TABLES):
            return []
        return [bad_row] if table == table_name else []

    monkeypatch.setattr(importer, "_table_rows", table_rows)
    monkeypatch.setattr(importer, "_load_column_types", lambda *_args: _postgres_types())
    backup = tmp_path / "must-not-exist.json.gz"
    with pytest.raises(TypeError, match="unsupported PostgreSQL backup type"):
        importer.run_import(
            di_db=Path("/di.db"), non_di_db=Path("/non.db"), focus_db=Path("/focus.db"),
            database_url="postgresql://redacted", target="xbray-api", execute=True,
            expected_sha256=DIGEST, expected_total_rows=1, backup_path=backup, repo_root=Path("/repo"),
        )
    assert not backup.exists()
    assert not any(query.startswith("INSERT") for query in conn.queries)


def test_migration_guard_rejects_incomplete_schema() -> None:
    class IncompleteCursor(_Cursor):
        def execute(self, query: str, params: object = ()) -> None:
            self.rows = [("001_observability",)]

    class IncompleteConnection(_Connection):
        def cursor(self) -> IncompleteCursor:
            return IncompleteCursor(self)

    with pytest.raises(ValueError, match="migrations are incomplete"):
        importer._check_migrations(IncompleteConnection())


def test_cli_requires_explicit_execution_target_and_redacts_errors(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str],
) -> None:
    from scripts import import_retrospective_production as cli

    base = ["--target-project", "xbray-api", "--di-source-db", "/di.db",
            "--non-di-source-db", "/non.db", "--focus-source-db", "/focus.db"]
    assert cli.main(["--target-project", "preview", *base[2:]]) == 2
    assert cli.main([*base, "--execute"]) == 2
    monkeypatch.setattr(cli, "run_import", lambda **_kwargs: (_ for _ in ()).throw(RuntimeError("dsn-secret")))
    assert cli.main(base) == 1
    assert "dsn-secret" not in capsys.readouterr().err


def test_cli_success_and_rollback_branches(monkeypatch: pytest.MonkeyPatch,
                                           tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    from scripts import import_retrospective_production as cli

    base = ["--target-project", "xbray-api", "--di-source-db", "/di.db",
            "--non-di-source-db", "/non.db", "--focus-source-db", "/focus.db"]
    monkeypatch.setattr(cli, "run_import", lambda **_kwargs: {"dry_run": True})
    assert cli.main(base) == 0
    assert '"dry_run": true' in capsys.readouterr().out
    monkeypatch.setattr(cli, "rollback_import", lambda **_kwargs: {"state": "rolled_back"})
    rollback_args = ["--target-project", "xbray-api", "--execute", "--confirm-production-target",
                     "xbray-api", "--rollback-batch", DIGEST, "--backup-path", str(tmp_path / "backup.gz")]
    assert cli.main(rollback_args) == 0
    assert '"state": "rolled_back"' in capsys.readouterr().out
    assert cli.main(["--target-project", "xbray-api", "--rollback-batch", DIGEST]) == 1
    assert "error_type" in capsys.readouterr().err
