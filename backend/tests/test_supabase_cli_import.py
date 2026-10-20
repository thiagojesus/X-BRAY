from __future__ import annotations

import gzip
import json
import subprocess
import stat
import struct
from datetime import date, datetime, timezone
from decimal import Decimal
from pathlib import Path
from unittest.mock import patch
from types import SimpleNamespace

import pytest

from db.migration_steps import v011_private_import_staging
from macro_score import supabase_cli_import as transport


def test_cli_json_skips_cli_banner_and_rejects_malformed_output() -> None:
    assert transport._cli_json('Initializing...\n{"rows":[{"x":1}]}')["rows"] == [
        {"x": 1}
    ]
    with pytest.raises(transport.SupabaseCliError):
        transport._cli_json("no result")
    with pytest.raises(transport.SupabaseCliError):
        transport._cli_json('{"rows":{}}')
    with pytest.raises(transport.SupabaseCliError, match="malformed"):
        transport._cli_json("{not-json}")
    with pytest.raises(transport.SupabaseCliError, match="no JSON"):
        transport._cli_json("[1, 2]")


def test_run_sql_uses_private_tempfile_and_removes_it() -> None:
    captured: dict[str, object] = {}

    def run(args: list[str], **kwargs: object) -> SimpleNamespace:
        query_file = Path(args[-1])
        captured["path"] = query_file
        captured["mode"] = stat.S_IMODE(query_file.stat().st_mode)
        captured["sql"] = query_file.read_text(encoding="utf-8")
        captured["args"] = args
        return SimpleNamespace(returncode=0, stdout='CLI banner\n{"rows":[]}')

    with (
        patch.object(transport.shutil, "which", return_value="/opt/bin/supabase"),
        patch.object(transport.subprocess, "run", side_effect=run),
    ):
        assert transport._run_sql("SELECT 1") == {"rows": []}
    assert captured["mode"] == 0o600
    assert captured["sql"] == "SELECT 1"
    assert "ajczxqrrqmvjzuthxjca" in captured["args"]
    assert not Path(captured["path"]).exists()


def test_run_sql_sanitizes_missing_cli_failure_and_timeout() -> None:
    with patch.object(transport.shutil, "which", return_value=None):
        with pytest.raises(transport.SupabaseCliError, match="not found"):
            transport._run_sql("SELECT 1")
    with (
        patch.object(transport.shutil, "which", return_value="/opt/bin/supabase"),
        patch.object(
            transport.subprocess,
            "run",
            return_value=SimpleNamespace(returncode=1, stdout="SECRET"),
        ),
    ):
        with pytest.raises(transport.SupabaseCliError) as error:
            transport._run_sql("SELECT 1")
        assert "SECRET" not in str(error.value)
    with (
        patch.object(transport.shutil, "which", return_value="/opt/bin/supabase"),
        patch.object(
            transport.subprocess,
            "run",
            side_effect=subprocess.TimeoutExpired("supabase", 1),
        ),
    ):
        with pytest.raises(transport.SupabaseCliError, match="timed out"):
            transport._run_sql("SELECT 1")


def test_sql_literal_quotes_strings_and_rejects_nul() -> None:
    assert transport._sql_literal("O'Brien") == "'O''Brien'"
    with pytest.raises(ValueError):
        transport._sql_literal("bad\x00value")


def test_sql_json_preserves_decimal_date_and_timestamp_semantics() -> None:
    value = {
        "amount": Decimal("0.10000000000000000001"),
        "day": date(2026, 10, 20),
        "timestamp": datetime(2026, 10, 20, tzinfo=timezone.utc),
    }
    result = transport._sql_json(value)
    assert "0.10000000000000000001" in result
    assert "2026-10-20" in result


def test_native_value_reconstructs_numeric_date_time_and_real() -> None:
    types = {
        ("retrospective_macro_values", "value"): "numeric",
        ("retrospective_di_observations", "trade_date"): "date",
        ("retrospective_di_observations", "rate"): "real",
        ("retrospective_di_observations", "fetched_at"): "timestamp with time zone",
    }
    assert transport._native_value(
        "retrospective_macro_values", "value", "0.10000000000000000001", types
    ) == Decimal("0.10000000000000000001")
    assert transport._native_value(
        "retrospective_di_observations", "trade_date", "2026-10-20", types
    ) == date(2026, 10, 20)
    real = transport._native_value(
        "retrospective_di_observations", "rate", "0.1", types
    )
    assert real == struct.unpack("!f", struct.pack("!f", 0.1))[0]
    timestamp = transport._native_value(
        "retrospective_di_observations",
        "fetched_at",
        "2026-10-20T12:00:00+00:00",
        types,
    )
    assert timestamp.tzinfo is not None
    with pytest.raises(ValueError):
        transport._native_value(
            "retrospective_di_observations", "trade_date", "bad-date", types
        )


def test_sql_call_rejects_large_payload_before_invoking_cli() -> None:
    with patch.object(transport.subprocess, "run") as run:
        with pytest.raises(ValueError, match="128 KiB"):
            transport._run_sql(" " * (transport.MAX_SQL_BYTES + 1))
        run.assert_not_called()


def test_external_backup_is_versioned_gzipped_and_private(tmp_path: Path) -> None:
    destination = tmp_path / "backup.json.gz"
    rows = {table: [] for table in transport.importer.TABLES}
    digest = transport._write_backup(destination, rows, {}, tmp_path / "repo")
    assert len(digest) == 64
    assert stat.S_IMODE(destination.stat().st_mode) == 0o600
    with gzip.open(destination, "rt", encoding="utf-8") as stream:
        payload = json.load(stream)
    assert payload["format_version"] == 2
    with pytest.raises(ValueError, match="backup"):
        transport._write_backup(destination, rows, {}, tmp_path / "repo")


def test_staging_rows_are_bounded_even_with_quote_heavy_values() -> None:
    table = "retrospective_macro_values"
    types = {(table, column): "text" for column in transport.importer.TABLES[table][0]}
    types[(table, "value")] = "real"
    row = {
        "component_key": "a'" * 40000,
        "reference_month": "2026-01",
        "value": 0.1,
        "unit": "u",
        "formula_version": "v",
        "source_basis": "retrospective_reference",
        "fetched_at": "2026-01-01T00:00:00+00:00",
        "inputs_json": "{}",
        "inputs_hash": "a" * 64,
        "reason": None,
    }
    with pytest.raises(ValueError, match="single source row"):
        transport._chunked_rows(table, [row], types, "a" * 64)


def test_empty_chunk_input_produces_no_staging_chunks() -> None:
    table = "retrospective_macro_values"
    assert transport._chunked_rows(table, [], {}, "a" * 64) == []


def test_discard_staging_batch_validates_and_verifies_exact_batch() -> None:
    with pytest.raises(ValueError, match="SHA-256"):
        transport.discard_staging_batch("bad-id")
    with patch.object(
        transport, "_run_sql", side_effect=[{}, {"rows": [{"row_count": "0"}]}]
    ) as sql:
        result = transport.discard_staging_batch("a" * 64)
    assert result["staging_discarded"] is True
    assert sql.call_count == 2


def test_preflight_fails_closed_when_migrations_missing() -> None:
    with patch.object(transport, "_run_sql", return_value={"rows": []}):
        with pytest.raises(ValueError, match="migrations"):
            transport._preflight()


def test_promotion_uses_fixed_tables_and_enforces_batch_guards() -> None:
    sql = transport._promotion_sql("a" * 64)
    assert len(sql.encode()) < transport.MAX_SQL_BYTES
    assert "pg_advisory_xact_lock" in sql
    assert "production identity changed" in sql
    assert "incomplete staged chunks" in sql
    assert "ON CONFLICT DO NOTHING" in sql
    assert "xbray_import_private" in sql
    assert "DROP TABLE" not in sql


def test_private_staging_migration_is_not_a_public_api_surface() -> None:
    statements = " ".join(v011_private_import_staging.POSTGRES_STATEMENTS).lower()
    assert v011_private_import_staging.SQLITE_STATEMENTS == ()
    assert "create schema if not exists xbray_import_private" in statements
    assert "revoke all on schema xbray_import_private from public" in statements
    assert "revoke all on schema xbray_import_private from anon" in statements
    assert "revoke all on schema xbray_import_private from authenticated" in statements
    assert "transport_version smallint" in statements
    assert "payload_md5" in statements


def test_run_supabase_import_dryrun_does_not_call_write_transport(
    tmp_path: Path,
) -> None:
    fake_rows = {table: [] for table in transport.importer.TABLES}
    manifest = {
        "bundle_sha256": "a" * 64,
        "total_rows": 0,
        "row_counts": {table: 0 for table in transport.importer.TABLES},
    }
    validation = {"point_in_time": False}
    guard = {
        "baselines": {
            table: {"row_count": 0, "fingerprint": "d41d8cd98f00b204e9800998ecf8427e"}
            for table in transport.importer.TABLES
        }
    }
    with (
        patch.object(
            transport.importer, "_source_bundle", return_value=(fake_rows, manifest)
        ),
        patch.object(transport.importer, "_validate_bundle", return_value=validation),
        patch.object(transport, "_preflight", return_value=({}, fake_rows, guard)),
        patch.object(transport, "_run_sql", return_value={"rows": []}) as run_sql,
    ):
        report = transport.run_supabase_import(
            di_db=tmp_path / "di.db",
            non_di_db=tmp_path / "macro.db",
            focus_db=tmp_path / "focus.db",
            target="xbray-api",
            execute=False,
            confirm_target=None,
            expected_sha256=None,
            expected_total_rows=None,
            backup_path=None,
            repo_root=tmp_path / "repo",
        )
    assert report["dry_run"] is True
    assert report["planned_inserts"] == {
        table: 0 for table in transport.importer.TABLES
    }
    run_sql.assert_called_once()
