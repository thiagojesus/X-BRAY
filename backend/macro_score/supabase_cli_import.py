"""Authenticated Supabase Management-API transport for the reviewed importer.

The CLI transports bounded SQL files; validation, source identity, numeric
normalization and backup encoding remain the reviewed production_import code.
"""

from __future__ import annotations

import gzip
import hashlib
import json
import os
import re
import shutil
import stat
import struct
import subprocess
import tempfile
from datetime import date, datetime, timezone
from decimal import Decimal
from pathlib import Path
from typing import Any, cast

from macro_score import production_import as importer

PROJECT_REF = "ajczxqrrqmvjzuthxjca"
EXPECTED_CYCLE = "39437294240"
EXPECTED_INPUTS_HASH = (
    "5008defc4015b22ae668e8cf2694df0c24600cda39aa57b907a95d7fc25773df"
)
MAX_SQL_BYTES = 128 * 1024
PAGE_SIZE = 400
MAX_SOURCE_ROWS = 6157
STAGING_SCHEMA = "xbray_import_private"
IMPORT_LOCK_KEY = 0x5842524159494D50
AUXILIARY_TABLES: dict[str, tuple[tuple[str, ...], tuple[str, ...]]] = {
    "production_data_import_batches": (
        ("batch_id", "source_manifest_json", "imported_at", "state"),
        ("batch_id",),
    ),
    "production_data_import_batch_rows": (
        ("batch_id", "table_name", "row_identity_json", "row_sha256"),
        ("batch_id", "table_name", "row_identity_json"),
    ),
}


class SupabaseCliError(RuntimeError):
    """Sanitized CLI transport failure; subprocess output is never propagated."""


def _canonical(value: object) -> str:
    return json.dumps(
        value,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
        allow_nan=False,
    )


def _wire_value(item: object) -> object:
    if isinstance(item, Decimal):
        return str(item)
    if isinstance(item, (date, datetime)):
        return item.isoformat()
    if isinstance(item, dict):
        mapping = cast(dict[object, object], item)
        return {str(key): _wire_value(value) for key, value in mapping.items()}
    if isinstance(item, (list, tuple)):
        sequence = cast(list[object] | tuple[object, ...], item)
        return [_wire_value(value) for value in sequence]
    return item


def _sql_literal(value: str) -> str:
    if "\x00" in value:
        raise ValueError("SQL transport rejects NUL characters")
    return "'" + value.replace("'", "''") + "'"


def _sql_json(value: object) -> str:
    return _sql_literal(_canonical(_wire_value(value))) + "::jsonb"


def _cli_json(stdout: str) -> dict[str, Any]:
    decoder = json.JSONDecoder()
    start = stdout.find("{")
    if start < 0:
        raise SupabaseCliError("Supabase CLI returned no JSON result")
    try:
        value, _end = decoder.raw_decode(stdout[start:])
    except json.JSONDecodeError as error:
        raise SupabaseCliError("Supabase CLI returned malformed JSON") from error
    if not isinstance(value, dict):
        raise SupabaseCliError("Supabase CLI returned an unexpected result shape")
    result = cast(dict[str, Any], value)
    if not isinstance(result.get("rows", []), list):
        raise SupabaseCliError("Supabase CLI returned an unexpected result shape")
    return result


def _run_sql(sql: str, *, cli: str = "supabase") -> dict[str, Any]:
    if len(sql.encode("utf-8")) > MAX_SQL_BYTES:
        raise ValueError("SQL transport payload exceeds the 128 KiB chunk limit")
    executable = shutil.which(cli)
    if executable is None:
        raise SupabaseCliError("Supabase CLI executable was not found")
    try:
        with tempfile.TemporaryDirectory(prefix="xbray-supabase-query-") as directory:
            workdir = Path(directory)
            os.chmod(workdir, stat.S_IRUSR | stat.S_IWUSR | stat.S_IXUSR)
            path = workdir / "query.sql"
            path.write_text(sql, encoding="utf-8")
            os.chmod(path, stat.S_IRUSR | stat.S_IWUSR)
            result = subprocess.run(
                [
                    executable,
                    "--workdir",
                    str(workdir),
                    "db",
                    "query",
                    "--linked",
                    "--project-ref",
                    PROJECT_REF,
                    "--output-format",
                    "json",
                    "--file",
                    str(path),
                ],
                check=False,
                capture_output=True,
                text=True,
                timeout=180,
                cwd=workdir,
            )
            if result.returncode:
                raise SupabaseCliError(
                    f"Supabase CLI SQL request failed (exit {result.returncode})"
                )
            return _cli_json(result.stdout)
    except subprocess.TimeoutExpired as error:
        raise SupabaseCliError("Supabase CLI SQL request timed out") from error


def _row_projection(table: str, column_types: dict[tuple[str, str], str]) -> str:
    columns = importer.TABLES[table][0]
    return ",".join(f'"{column}"::text AS "{column}"' for column in columns)


def _native_value(
    table: str, column: str, value: object, types: dict[tuple[str, str], str]
) -> object:
    if value is None:
        return None
    pg_type = types[(table, column)]
    text = str(value)
    if column in importer.FLOAT_COLUMNS.get(table, frozenset()):
        if pg_type in {"numeric", "decimal"}:
            return Decimal(text)
        parsed = float(text)
        return (
            struct.unpack("!f", struct.pack("!f", parsed))[0]
            if pg_type == "real"
            else parsed
        )
    if column in importer.DATE_COLUMNS.get(table, frozenset()) and pg_type == "date":
        return date.fromisoformat(text)
    if (
        column in importer.TIMESTAMP_COLUMNS.get(table, frozenset())
        and pg_type == "timestamp with time zone"
    ):
        return datetime.fromisoformat(text.replace("Z", "+00:00"))
    return value


def _types() -> dict[tuple[str, str], str]:
    wanted = ",".join(_sql_literal(table) for table in importer.TABLES)
    rows = _run_sql(
        "SELECT table_name,column_name,data_type FROM information_schema.columns "
        f"WHERE table_schema='public' AND table_name IN ({wanted}) ORDER BY table_name,ordinal_position"
    ).get("rows", [])
    raw_types = {
        (str(row["table_name"]), str(row["column_name"])): str(row["data_type"]).lower()
        for row in rows
    }
    return importer.validate_column_types(raw_types)


def _baseline(table: str) -> tuple[int, str]:
    if table in importer.TABLES:
        _columns, keys = importer.TABLES[table]
    else:
        _columns, keys = AUXILIARY_TABLES[table]
    order = ",".join(f't."{key}"' for key in keys)
    sql = (
        f"SELECT count(*)::text AS row_count, md5(coalesce(string_agg(to_jsonb(t)::text, "
        f"E'\\n' ORDER BY {order}),'')) AS fingerprint FROM public.\"{table}\" t"
    )
    row = _run_sql(sql)["rows"][0]
    return int(row["row_count"]), str(row["fingerprint"])


def _read_auxiliary(table: str) -> list[dict[str, object]]:
    columns, keys = AUXILIARY_TABLES[table]
    projection = ",".join(f'"{column}"::text AS "{column}"' for column in columns)
    order = ",".join(f'"{key}"' for key in keys)
    result = _run_sql(f'SELECT {projection} FROM public."{table}" ORDER BY {order}')
    return [
        {column: row.get(column) for column in columns}
        for row in result.get("rows", [])
    ]


def _read_table(
    table: str, types: dict[tuple[str, str], str]
) -> list[dict[str, object]]:
    columns, keys = importer.TABLES[table]
    projection = _row_projection(table, types)
    order = ",".join(f'"{key}"' for key in keys)
    rows: list[dict[str, object]] = []
    offset = 0
    while True:
        result = _run_sql(
            f'SELECT {projection} FROM public."{table}" ORDER BY {order} '
            f"LIMIT {PAGE_SIZE} OFFSET {offset}"
        )
        page = result.get("rows", [])
        if not page:
            return rows
        for record in page:
            rows.append(
                {
                    column: _native_value(table, column, record.get(column), types)
                    for column in columns
                }
            )
        offset += len(page)
        if len(page) < PAGE_SIZE:
            return rows


def _preflight() -> tuple[
    dict[tuple[str, str], str], dict[str, list[dict[str, object]]], dict[str, Any]
]:
    migrations = {
        str(row["version"])
        for row in _run_sql(
            "SELECT version FROM public.schema_migrations ORDER BY version"
        ).get("rows", [])
    }
    if not importer.REQUIRED_MIGRATIONS.issubset(migrations):
        raise ValueError("production database migrations are incomplete")
    types = _types()
    all_target_tables = tuple(importer.TABLES) + tuple(AUXILIARY_TABLES)
    before = {table: _baseline(table) for table in all_target_tables}
    existing = {table: _read_table(table, types) for table in importer.TABLES}
    auxiliary = {table: _read_auxiliary(table) for table in AUXILIARY_TABLES}
    baselines = {
        table: dict(zip(("row_count", "fingerprint"), _baseline(table), strict=True))
        for table in all_target_tables
    }
    if any(
        (int(baselines[table]["row_count"]), str(baselines[table]["fingerprint"]))
        != before[table]
        for table in all_target_tables
    ):
        raise ValueError(
            "database changed while the read-only snapshot was being captured"
        )
    guard = _run_sql(
        "SELECT calculation_cycle_id,inputs_hash FROM public.macro_score_snapshots "
        "WHERE origin='production_refresh' ORDER BY score_month DESC,calculated_at DESC LIMIT 1"
    ).get("rows", [])
    if (
        not guard
        or guard[0]["calculation_cycle_id"] != EXPECTED_CYCLE
        or guard[0]["inputs_hash"] != EXPECTED_INPUTS_HASH
    ):
        raise ValueError("confirmed production identity changed; refresh preflight")
    staging_schema = _run_sql(
        "SELECT count(*)::text AS table_count FROM information_schema.tables "
        "WHERE table_schema='xbray_import_private' AND table_name IN ('batches','rows','chunks')"
    ).get("rows", [])
    schema_fingerprint_rows = _run_sql(
        "SELECT md5(coalesce(string_agg(table_name||'.'||column_name||':'||data_type, "
        "E'\\n' ORDER BY table_name,ordinal_position),'')) AS fingerprint "
        "FROM information_schema.columns WHERE table_schema='public' AND table_name IN ("
        + ",".join(_sql_literal(table) for table in importer.TABLES)
        + ")"
    ).get("rows", [])
    if not schema_fingerprint_rows:
        raise ValueError("target schema fingerprint is unavailable")
    return (
        types,
        existing,
        {
            "baselines": baselines,
            "cycle_id": EXPECTED_CYCLE,
            "inputs_hash": EXPECTED_INPUTS_HASH,
            "schema_migrations": sorted(migrations),
            "schema_fingerprint": schema_fingerprint_rows[0]["fingerprint"],
            "staging_ready": bool(
                staging_schema and staging_schema[0]["table_count"] == "3"
            ),
            "auxiliary": auxiliary,
        },
    )


def _write_backup(
    path: Path,
    existing: dict[str, list[dict[str, object]]],
    auxiliary: dict[str, list[dict[str, object]]],
    repo_root: Path,
) -> str:
    target = path.resolve()
    if (
        not path.is_absolute()
        or target.is_relative_to(repo_root.resolve())
        or target.exists()
    ):
        raise ValueError("backup must be a new absolute path outside the repository")
    payload = importer.canonical_json(
        {
            "format": "xbray-production-ledger-backup-v1",
            "format_version": 2,
            "tables": importer.encode_backup({**existing, **auxiliary}),
        }
    ).encode()
    target.parent.mkdir(parents=True, exist_ok=True)
    fd = os.open(
        target, os.O_WRONLY | os.O_CREAT | os.O_EXCL, stat.S_IRUSR | stat.S_IWUSR
    )
    try:
        with (
            os.fdopen(fd, "wb") as raw,
            gzip.GzipFile(fileobj=raw, mode="wb", mtime=0) as stream,
        ):
            stream.write(payload)
    except Exception:
        target.unlink(missing_ok=True)
        raise
    return hashlib.sha256(payload).hexdigest()


def _chunked_rows(
    table: str,
    rows: list[dict[str, object]],
    types: dict[tuple[str, str], str],
    batch_id: str,
    *,
    purpose: str = "insert",
    ordinal_offset: int = 0,
) -> list[tuple[str, list[dict[str, object]]]]:
    columns, keys = importer.TABLES[table]
    chunks: list[tuple[str, list[dict[str, object]]]] = []
    current: list[dict[str, object]] = []
    current_bytes = 0
    for index, row in enumerate(rows):
        ordinal = ordinal_offset + index
        payload: dict[str, object] = {
            column: importer.bind_value(table, column, row[column], types)
            for column in columns
        }
        identity = importer.semantic_identity(table, row, keys, types)
        digest = importer.semantic_row_hash(table, row, types)
        record: dict[str, object] = {
            "ordinal": ordinal,
            "purpose": purpose,
            "payload": payload,
            "identity": identity,
            "sha256": digest,
        }
        size = len(_canonical(_wire_value(record)).encode())
        if size * 2 + 4096 > MAX_SQL_BYTES:
            raise ValueError("single source row exceeds bounded staging statement size")
        if current and (current_bytes + size) * 2 + 4096 > MAX_SQL_BYTES:
            chunks.append((table, current))
            current, current_bytes = [], 0
        current.append(record)
        current_bytes += size
    if current:
        chunks.append((table, current))
    return chunks


def _resolved_manifest(
    source_manifest: dict[str, object],
    rows: dict[str, list[dict[str, object]]],
    reused: list[dict[str, object]],
    rebased_macro_rows: int,
) -> dict[str, object]:
    if not reused:
        return source_manifest
    source_digest = str(source_manifest["bundle_sha256"])
    resolved: dict[str, object] = {
        **source_manifest,
        "manifest_version": "xbray-retrospective-production-import-v2",
        "source_bundle_sha256": source_digest,
        "reconciliation": {
            "policy": "preserve_exact_production_vintage_provenance_v1",
            "reused_vintage_rows": reused,
            "validation_rows_sha256": hashlib.sha256(
                "\n".join(
                    f"{item['identity']}\t{item['source_row_sha256']}"
                    for item in sorted(reused, key=lambda row: str(row["identity"]))
                ).encode()
            ).hexdigest(),
            "rebased_macro_dependency_rows": rebased_macro_rows,
        },
    }
    resolved.pop("bundle_sha256", None)
    digest_rows = {
        table: sorted(table_rows, key=importer.canonical_json)
        for table, table_rows in rows.items()
    }
    resolved["bundle_sha256"] = hashlib.sha256(
        _canonical({"manifest": resolved, "rows": digest_rows}).encode()
    ).hexdigest()
    return resolved


def _insert_stage(
    batch_id: str, table: str, chunk_index: int, records: list[dict[str, object]]
) -> None:
    values: list[str] = []
    for record in records:
        payload_sql = _sql_json(record["payload"])
        values.append(
            "("
            + ",".join(
                (
                    _sql_literal(batch_id),
                    _sql_literal(table),
                    str(chunk_index),
                    str(record["ordinal"]),
                    payload_sql,
                    f"md5(({payload_sql})::text)",
                    _sql_literal(str(record["identity"])),
                    _sql_literal(str(record["sha256"])),
                    _sql_literal(str(record["purpose"])),
                )
            )
            + ")"
        )
    chunk_body = ",".join(values)
    chunk_insert = (
        "INSERT INTO xbray_import_private.chunks "
        "(batch_id,table_name,chunk_index,row_count,chunk_sha256,purpose) "
        "SELECT batch_id,table_name,chunk_index,count(*),encode(sha256(convert_to("
        "jsonb_agg(jsonb_build_array(ordinal,payload_json,row_identity_json,row_sha256,purpose) "
        "ORDER BY ordinal)::text,'UTF8')),'hex'),purpose FROM xbray_import_private.rows "
        f"WHERE batch_id={_sql_literal(batch_id)} AND table_name={_sql_literal(table)} "
        f"AND chunk_index={chunk_index} GROUP BY batch_id,table_name,chunk_index,purpose "
        "ON CONFLICT DO NOTHING"
    )
    sql = (
        "BEGIN; INSERT INTO xbray_import_private.rows "
        "(batch_id,table_name,chunk_index,ordinal,payload_json,payload_md5,row_identity_json,row_sha256,purpose) VALUES "
        + chunk_body
        + " ON CONFLICT DO NOTHING; "
        + chunk_insert
        + "; COMMIT;"
    )
    if len(sql.encode()) > MAX_SQL_BYTES:
        raise ValueError("generated staging SQL exceeded its byte budget")
    _run_sql(sql)
    check = _run_sql(
        "SELECT ordinal,payload_json,row_identity_json,row_sha256,payload_md5,purpose "
        "FROM xbray_import_private.rows "
        f"WHERE batch_id={_sql_literal(batch_id)} AND table_name={_sql_literal(table)} "
        f"AND chunk_index={chunk_index} ORDER BY ordinal"
    ).get("rows", [])
    if len(check) != len(records):
        raise ValueError("staging chunk verification failed")
    for actual, expected in zip(check, records, strict=True):
        actual_payload = actual["payload_json"]
        if isinstance(actual_payload, str):
            actual_payload = json.loads(actual_payload)
        if (
            int(actual["ordinal"]) != expected["ordinal"]
            or actual_payload != _wire_value(expected["payload"])
            or actual["row_identity_json"] != expected["identity"]
            or actual["row_sha256"] != expected["sha256"]
            or actual["purpose"] != expected["purpose"]
        ):
            raise ValueError(
                "staging chunk payload does not match the validated source"
            )
    chunk_check = _run_sql(
        "SELECT row_count,chunk_sha256,chunk_sha256=encode(sha256(convert_to("
        "(SELECT jsonb_agg(jsonb_build_array(ordinal,payload_json,row_identity_json,row_sha256,purpose) "
        "ORDER BY ordinal)::text FROM xbray_import_private.rows r "
        "WHERE r.batch_id=c.batch_id AND r.table_name=c.table_name "
        "AND r.chunk_index=c.chunk_index),'UTF8')),'hex') AS integrity_ok "
        "FROM xbray_import_private.chunks c "
        f"WHERE batch_id={_sql_literal(batch_id)} AND table_name={_sql_literal(table)} "
        f"AND chunk_index={chunk_index}"
    ).get("rows", [])
    if (
        not chunk_check
        or int(chunk_check[0]["row_count"]) != len(records)
        or chunk_check[0]["integrity_ok"] is not True
    ):
        raise ValueError("staging chunk manifest does not match the resumed chunk")


def _promotion_sql(batch_id: str) -> str:
    all_target_tables = tuple(importer.TABLES) + tuple(AUXILIARY_TABLES)
    lock_tables = ",".join(f'public."{table}"' for table in all_target_tables)
    body = [
        "DO $xbray$ DECLARE b RECORD; r RECORD; claim JSONB; n INTEGER; actual_count BIGINT; actual_fp TEXT; BEGIN",
        f"PERFORM pg_advisory_xact_lock({IMPORT_LOCK_KEY});",
        "SELECT * INTO b FROM xbray_import_private.batches WHERE batch_id="
        + _sql_literal(batch_id)
        + " FOR UPDATE;",
        "IF NOT FOUND OR b.state <> 'ready' THEN RAISE EXCEPTION 'import staging batch is not ready'; END IF;",
        "IF b.transport_version <> 1 OR b.target_ref <> "
        + _sql_literal(PROJECT_REF)
        + " OR b.batch_id <> b.manifest_json::jsonb->>'bundle_sha256' THEN "
        "RAISE EXCEPTION 'staging target or source hash mismatch'; END IF;",
        "IF (SELECT md5(coalesce(string_agg(table_name||'.'||column_name||':'||data_type, "
        "E'\\n' ORDER BY table_name,ordinal_position),'')) FROM information_schema.columns "
        "WHERE table_schema='public' AND table_name IN ("
        + ",".join(_sql_literal(table) for table in importer.TABLES)
        + ")) "
        "<> b.baseline_json->>'schema_fingerprint' THEN RAISE EXCEPTION 'target schema changed'; END IF;",
        (
            "IF EXISTS (SELECT 1 FROM xbray_import_private.rows WHERE batch_id=b.batch_id "
            "AND md5(payload_json::text) <> payload_md5) THEN RAISE EXCEPTION "
            "'staging payload integrity failure'; END IF;"
        ),
        (
            "IF EXISTS (SELECT 1 FROM xbray_import_private.chunks c WHERE c.batch_id=b.batch_id "
            "AND c.chunk_sha256 <> encode(sha256(convert_to((SELECT jsonb_agg("
            "jsonb_build_array(s.ordinal,s.payload_json,s.row_identity_json,s.row_sha256,s.purpose) "
            "ORDER BY s.ordinal)::text FROM xbray_import_private.rows s WHERE s.batch_id=c.batch_id "
            "AND s.table_name=c.table_name AND s.chunk_index=c.chunk_index),'UTF8')),'hex')) THEN "
            "RAISE EXCEPTION 'staged chunk integrity failure'; END IF;"
        ),
        f"LOCK TABLE {lock_tables} IN SHARE ROW EXCLUSIVE MODE;",
        "IF (SELECT calculation_cycle_id || ':' || inputs_hash FROM public.macro_score_snapshots "
        "WHERE origin='production_refresh' ORDER BY score_month DESC,calculated_at DESC LIMIT 1) "
        "<> '" + EXPECTED_CYCLE + ":" + EXPECTED_INPUTS_HASH + "' THEN "
        "RAISE EXCEPTION 'production identity changed'; END IF;",
    ]
    for table in all_target_tables:
        _columns, keys = (
            importer.TABLES[table]
            if table in importer.TABLES
            else AUXILIARY_TABLES[table]
        )
        order = ",".join(f't."{key}"' for key in keys)
        baseline = (
            "SELECT count(*),md5(coalesce(string_agg(to_jsonb(t)::text,E'\\n' ORDER BY "
            f"{order}),'')) FROM public.\"{table}\" t"
        )
        body.append(f"EXECUTE {_sql_literal(baseline)} INTO actual_count,actual_fp;")
        body.append(
            "IF actual_count <> (b.baseline_json->'baselines'->"
            + _sql_literal(table)
            + "->>'row_count')::bigint OR actual_fp <> b.baseline_json->'baselines'->"
            + _sql_literal(table)
            + "->>'fingerprint' THEN RAISE EXCEPTION 'target baseline changed'; END IF;"
        )
    body += [
        (
            "IF b.manifest_json::jsonb ? 'reconciliation' THEN "
            "IF (SELECT count(*) FROM xbray_import_private.rows WHERE batch_id=b.batch_id AND purpose='validation' AND table_name='observation_vintages') <> jsonb_array_length(b.manifest_json::jsonb->'reconciliation'->'reused_vintage_rows') THEN RAISE EXCEPTION 'incomplete vintage validation rows'; END IF; "
            "IF (SELECT count(DISTINCT row_identity_json) FROM xbray_import_private.rows WHERE batch_id=b.batch_id AND purpose='validation' AND table_name='observation_vintages') <> jsonb_array_length(b.manifest_json::jsonb->'reconciliation'->'reused_vintage_rows') THEN RAISE EXCEPTION 'duplicate vintage validation identity'; END IF; "
            "IF (SELECT count(DISTINCT value->>'identity') FROM jsonb_array_elements(b.manifest_json::jsonb->'reconciliation'->'reused_vintage_rows')) <> jsonb_array_length(b.manifest_json::jsonb->'reconciliation'->'reused_vintage_rows') THEN RAISE EXCEPTION 'duplicate vintage reuse claim'; END IF; "
            "IF encode(sha256(convert_to((SELECT string_agg(row_identity_json||E'\\t'||row_sha256,E'\\n' ORDER BY row_identity_json) FROM xbray_import_private.rows WHERE batch_id=b.batch_id AND purpose='validation' AND table_name='observation_vintages'),'UTF8')),'hex') <> b.manifest_json::jsonb->'reconciliation'->>'validation_rows_sha256' THEN RAISE EXCEPTION 'vintage validation source root mismatch'; END IF; "
            "FOR claim IN SELECT value FROM jsonb_array_elements(b.manifest_json::jsonb->'reconciliation'->'reused_vintage_rows') LOOP "
            "IF (SELECT count(*) FROM xbray_import_private.rows s WHERE s.batch_id=b.batch_id AND s.purpose='validation' AND s.table_name='observation_vintages' AND s.row_identity_json=claim->>'identity' AND s.row_sha256=claim->>'source_row_sha256' AND s.payload_json->>'payload_hash'=claim->>'payload_hash') <> 1 THEN RAISE EXCEPTION 'fabricated or duplicate vintage validation claim'; END IF; "
            "IF NOT EXISTS (SELECT 1 FROM public.observation_vintages t JOIN xbray_import_private.rows s ON s.batch_id=b.batch_id AND s.purpose='validation' AND s.table_name='observation_vintages' AND s.row_identity_json=claim->>'identity' CROSS JOIN LATERAL jsonb_populate_record(NULL::public.observation_vintages,s.payload_json) src CROSS JOIN LATERAL jsonb_populate_record(NULL::public.observation_vintages,jsonb_build_object('first_seen_at',claim->'source_timestamps'->'first_seen_at','available_at',claim->'source_timestamps'->'available_at')) source_claim CROSS JOIN LATERAL jsonb_populate_record(NULL::public.observation_vintages,jsonb_build_object('first_seen_at',claim->'target_timestamps'->'first_seen_at','available_at',claim->'target_timestamps'->'available_at')) target_claim WHERE t.provider=src.provider AND t.series_key=src.series_key AND t.reference_period=src.reference_period AND t.dimensions_hash=src.dimensions_hash AND t.payload_hash=src.payload_hash AND (to_jsonb(t)-'id'-'first_seen_at'-'available_at')=(to_jsonb(src)-'id'-'first_seen_at'-'available_at') AND t.first_seen_at=target_claim.first_seen_at AND t.available_at=target_claim.available_at AND src.first_seen_at=source_claim.first_seen_at AND src.available_at=source_claim.available_at) THEN RAISE EXCEPTION 'production vintage does not match exact reuse claim'; END IF; "
            "END LOOP; END IF;"
        )
    ]
    body += [
        (
            "FOR r IN SELECT key,value FROM jsonb_each_text(b.row_counts_json) LOOP "
            "IF (SELECT count(*) FROM xbray_import_private.rows WHERE batch_id=b.batch_id "
            "AND table_name=r.key AND purpose='insert') <> r.value::bigint THEN RAISE EXCEPTION 'incomplete staged rows'; END IF; "
            "IF coalesce((SELECT sum(row_count) FROM xbray_import_private.chunks "
            "WHERE batch_id=b.batch_id AND table_name=r.key AND purpose='insert'),0) <> r.value::bigint THEN "
            "RAISE EXCEPTION 'incomplete staged chunks'; END IF; END LOOP;"
        ),
        "FOR r IN SELECT * FROM xbray_import_private.rows WHERE batch_id=b.batch_id AND purpose='insert' ORDER BY table_name,ordinal LOOP",
    ]
    for table, (columns, _keys) in importer.TABLES.items():
        insert = (
            f'INSERT INTO public."{table}" ({",".join(f'"{c}"' for c in columns)}) '
            f"SELECT {','.join(f'"{c}"' for c in columns)} "
            f'FROM jsonb_populate_record(NULL::public."{table}",$1) '
            "ON CONFLICT DO NOTHING"
        )
        body.append(
            f"IF r.table_name={_sql_literal(table)} THEN EXECUTE {_sql_literal(insert)} USING r.payload_json; END IF;"
        )
        body.append(
            "GET DIAGNOSTICS n = ROW_COUNT; IF r.table_name="
            + _sql_literal(table)
            + " AND n <> 1 THEN RAISE EXCEPTION 'staged append-only row conflicted'; END IF;"
        )
    body += [
        (
            "INSERT INTO public.production_data_import_batch_rows(batch_id,table_name,row_identity_json,row_sha256) "
            "VALUES (b.batch_id,r.table_name,r.row_identity_json,r.row_sha256);"
        ),
        "END LOOP;",
        (
            "INSERT INTO public.production_data_import_batches(batch_id,source_manifest_json,imported_at,state) "
            "VALUES (b.batch_id,b.manifest_json,now()::text,'committed');"
        ),
        "DELETE FROM xbray_import_private.batches WHERE batch_id=b.batch_id;",
        "END $xbray$;",
    ]
    sql = "BEGIN; " + " ".join(body) + " COMMIT;"
    if len(sql.encode()) > MAX_SQL_BYTES:
        raise ValueError("atomic promotion SQL exceeded the Management API limit")
    return sql


def discard_staging_batch(batch_id: str) -> dict[str, object]:
    """Remove only one uncommitted private staging batch; never touch live ledgers."""
    if not re.fullmatch(r"[0-9a-f]{64}", batch_id):
        raise ValueError("staging cleanup requires an exact bundle SHA-256")
    literal = _sql_literal(batch_id)
    sql = (
        "BEGIN; DO $cleanup$ DECLARE state_value TEXT; BEGIN "
        f"PERFORM pg_advisory_xact_lock({IMPORT_LOCK_KEY}); "
        f"SELECT state INTO state_value FROM xbray_import_private.batches WHERE batch_id={literal} FOR UPDATE; "
        "IF state_value IS NOT NULL THEN "
        f"IF EXISTS (SELECT 1 FROM public.production_data_import_batches WHERE batch_id={literal}) THEN "
        "RAISE EXCEPTION 'committed import cannot be discarded'; END IF; "
        "IF state_value NOT IN ('staging','ready') THEN RAISE EXCEPTION 'unknown staging state'; END IF; "
        f"DELETE FROM xbray_import_private.batches WHERE batch_id={literal}; "
        "END IF; END $cleanup$; COMMIT;"
    )
    _run_sql(sql)
    remaining = _run_sql(
        "SELECT count(*)::text AS row_count FROM xbray_import_private.batches WHERE batch_id="
        + literal
    ).get("rows", [])
    if remaining and int(remaining[0]["row_count"]) != 0:
        raise SupabaseCliError("staging batch cleanup could not be verified")
    return {"target_ref": PROJECT_REF, "batch_id": batch_id, "staging_discarded": True}


def run_supabase_import(
    *,
    di_db: Path,
    non_di_db: Path,
    focus_db: Path,
    target: str,
    execute: bool,
    confirm_target: str | None,
    expected_sha256: str | None,
    expected_total_rows: int | None,
    backup_path: Path | None,
    repo_root: Path,
    reuse_equivalent_vintages: bool = False,
    expected_source_sha256: str | None = None,
    expected_planned_rows: int | None = None,
) -> dict[str, object]:
    if target != importer.EXPECTED_TARGET:
        raise ValueError("unexpected import target")
    if execute and (
        confirm_target != importer.EXPECTED_TARGET
        or os.environ.get(importer.IMPORT_TARGET_ENV) != "xbray-api-production"
    ):
        raise ValueError(
            "explicit production confirmation and target environment are required"
        )
    rows, source_manifest = importer.source_bundle(di_db, non_di_db, focus_db)
    source_rows: dict[str, list[dict[str, object]]] = {
        table: [dict(row) for row in table_rows] for table, table_rows in rows.items()
    }
    now = datetime.now(timezone.utc)
    validation = importer.validate_bundle(rows, now)
    total_rows = source_manifest.get("total_rows")
    if not isinstance(total_rows, int) or total_rows > MAX_SOURCE_ROWS:
        raise ValueError(
            "validated source bundle exceeds the fixed transport row ceiling"
        )
    types, existing, guard = _preflight()
    plan, reused = importer.plan_import_reusing_equivalent_vintages(
        rows, existing, types, allow_reuse=reuse_equivalent_vintages, now=now
    )
    rebased_macro_rows = importer.rebase_vintage_dependencies(rows, reused)
    if reused:
        validation = importer.validate_bundle(rows, now)
        plan, confirmed_reused = importer.plan_import_reusing_equivalent_vintages(
            rows, existing, types, allow_reuse=True, now=now
        )
        if confirmed_reused != reused:
            raise ValueError(
                "vintage reconciliation changed while rebasing dependencies"
            )
    manifest = _resolved_manifest(source_manifest, rows, reused, rebased_macro_rows)
    inserted = {table: len(plan[table]) for table in importer.TABLES}
    planned_total = sum(inserted.values())
    if execute and (
        expected_sha256 != manifest["bundle_sha256"]
        or expected_total_rows != total_rows
        or (reused and expected_planned_rows != planned_total)
        or (
            expected_planned_rows is not None and expected_planned_rows != planned_total
        )
        or (reused and expected_source_sha256 != source_manifest["bundle_sha256"])
        or backup_path is None
    ):
        raise ValueError(
            "execution requires exact source/resolved manifest and row-count confirmation plus an external backup"
        )
    already = _run_sql(
        "SELECT batch_id,state FROM public.production_data_import_batches WHERE batch_id IN ("
        + _sql_literal(str(manifest["bundle_sha256"]))
        + ","
        + _sql_literal(str(source_manifest["bundle_sha256"]))
        + ") OR source_manifest_json::jsonb->>'source_bundle_sha256'="
        + _sql_literal(str(source_manifest["bundle_sha256"]))
    ).get("rows", [])
    if already and already[0]["state"] != "committed":
        raise ValueError("matching batch exists in a non-committed state")
    if already:
        inserted = {table: 0 for table in importer.TABLES}
    result: dict[str, object] = {
        "dry_run": not execute,
        "target": target,
        "target_ref": PROJECT_REF,
        "bundle_sha256": manifest["bundle_sha256"],
        "source_bundle_sha256": source_manifest["bundle_sha256"],
        "total_rows": manifest["total_rows"],
        "planned_insert_rows": planned_total,
        "reused_equivalent_vintage_rows": len(reused),
        "rebased_macro_dependency_rows": rebased_macro_rows,
        "already_imported_batch_id": already[0]["batch_id"] if already else None,
        "source_row_counts": manifest["row_counts"],
        "planned_inserts": inserted,
        "remaining_insert_rows": sum(inserted.values()),
        "baseline": guard["baselines"],
        "already_imported": bool(already),
        "point_in_time": validation["point_in_time"],
    }
    if not execute or already:
        return result
    migration_values = guard.get("schema_migrations")
    if not isinstance(migration_values, list) or not all(
        isinstance(item, str) for item in cast(list[object], migration_values)
    ):
        raise ValueError("target migration metadata is malformed")
    auxiliary_values = guard.get("auxiliary")
    if not isinstance(auxiliary_values, dict):
        raise TypeError("backup snapshot metadata has an invalid type")
    if (
        not guard["staging_ready"]
        or "011_private_import_staging" not in migration_values
        or (reused and "012_import_validation_rows" not in migration_values)
    ):
        raise ValueError(
            "private staging migrations 011/012 must be deployed before execution"
        )
    if not backup_path:
        raise ValueError("execution requires an external backup path")
    backup_sha = _write_backup(
        backup_path,
        existing,
        cast(dict[str, list[dict[str, object]]], auxiliary_values),
        repo_root,
    )
    batch_id = str(manifest["bundle_sha256"])
    manifest_json = _canonical(manifest)
    batch_sql = (
        "INSERT INTO xbray_import_private.batches "
        "(batch_id,target_ref,transport_version,manifest_json,baseline_json,row_counts_json,state) "
        f"VALUES ({_sql_literal(batch_id)},{_sql_literal(PROJECT_REF)},1,{_sql_literal(manifest_json)},"
        f"{_sql_json(guard)},{_sql_json({table: len(plan[table]) for table in importer.TABLES})},'staging') "
        "ON CONFLICT DO NOTHING"
    )
    _run_sql(batch_sql)
    manifest_check = _run_sql(
        "SELECT target_ref,manifest_json,baseline_json,row_counts_json,state "
        "FROM xbray_import_private.batches WHERE batch_id=" + _sql_literal(batch_id)
    ).get("rows", [])
    if manifest_check:
        stored_baseline = manifest_check[0]["baseline_json"]
        stored_counts = manifest_check[0]["row_counts_json"]
        if isinstance(stored_baseline, str):
            stored_baseline = json.loads(stored_baseline)
        if isinstance(stored_counts, str):
            stored_counts = json.loads(stored_counts)
        expected_counts = {table: len(plan[table]) for table in importer.TABLES}
        if stored_baseline != _wire_value(guard) or stored_counts != expected_counts:
            raise ValueError(
                "incomplete staging batch belongs to a different target snapshot"
            )
    if (
        not manifest_check
        or manifest_check[0]["target_ref"] != PROJECT_REF
        or manifest_check[0]["manifest_json"] != manifest_json
        or manifest_check[0]["state"] not in {"staging", "ready"}
    ):
        raise ValueError("staging manifest conflicts with this validated bundle")
    for table in importer.TABLES:
        chunks = _chunked_rows(table, plan[table], types, batch_id)
        for index, (_chunk_table, records) in enumerate(chunks):
            _insert_stage(batch_id, table, index, records)
    if reused:
        vintage_keys = importer.TABLES["observation_vintages"][1]
        vintage_by_identity: dict[str, dict[str, object]] = {}
        for source_vintage in source_rows["observation_vintages"]:
            source_identity = importer.semantic_identity(
                "observation_vintages", source_vintage, vintage_keys, types
            )
            if source_identity in vintage_by_identity:
                raise ValueError("duplicate source observation_vintages identity")
            vintage_by_identity[source_identity] = source_vintage
        validation_rows: list[dict[str, object]] = []
        for claim in reused:
            identity = str(claim["identity"])
            source_vintage = vintage_by_identity.get(identity)
            if source_vintage is None:
                raise ValueError("reused vintage claim has no exact source identity")
            if (
                importer.semantic_row_hash(
                    "observation_vintages", source_vintage, types
                )
                != claim["source_row_sha256"]
                or source_vintage["payload_hash"] != claim["payload_hash"]
            ):
                raise ValueError("reused vintage source proof changed before staging")
            validation_rows.append(source_vintage)
        validation_chunks = _chunked_rows(
            "observation_vintages",
            validation_rows,
            types,
            batch_id,
            purpose="validation",
            ordinal_offset=int(total_rows),
        )
        for index, (_table, records) in enumerate(validation_chunks):
            _insert_stage(batch_id, "observation_vintages", index + 100000, records)
    _run_sql(
        "UPDATE xbray_import_private.batches SET state='ready' WHERE batch_id="
        + _sql_literal(batch_id)
        + " AND state='staging'"
    )
    _run_sql(_promotion_sql(batch_id))
    final = _run_sql(
        "SELECT state FROM public.production_data_import_batches WHERE batch_id="
        + _sql_literal(batch_id)
    ).get("rows", [])
    if not final or final[0]["state"] != "committed":
        raise SupabaseCliError("promotion completed without a committed journal record")
    result["backup_sha256"] = backup_sha
    result["state"] = "committed"
    return result
