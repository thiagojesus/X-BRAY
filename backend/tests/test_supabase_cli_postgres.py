"""PostgreSQL 17 integration coverage for atomic staged promotion.

Run against a disposable database only:
TEST_DATABASE_URL=postgresql://.../xbray_test pytest tests/test_supabase_cli_postgres.py
"""

from __future__ import annotations

import hashlib
import json
import os
from copy import deepcopy
from datetime import datetime
from pathlib import Path
from typing import Any
from unittest.mock import patch
from uuid import uuid4

import psycopg
from psycopg import sql
import pytest

from db.migrations import run_migrations
from macro_score import production_import as importer
from macro_score import supabase_cli_import as adapter


def _database() -> psycopg.Connection[Any] | None:
    url = os.environ.get("TEST_DATABASE_URL")
    if not url:
        pytest.skip("requires a disposable PostgreSQL 17 TEST_DATABASE_URL")
    if not url.rsplit("/", 1)[-1].split("?", 1)[0].endswith("_test"):
        pytest.fail("TEST_DATABASE_URL must point to an isolated *_test database")
    return psycopg.connect(url)


def _schema_fingerprint(connection: psycopg.Connection[Any]) -> str:
    tables = ",".join("%s" for _ in importer.TABLES)
    return connection.execute(
        "SELECT md5(coalesce(string_agg(table_name||'.'||column_name||':'||data_type, "
        "E'\\n' ORDER BY table_name,ordinal_position),'')) "
        "FROM information_schema.columns WHERE table_schema='public' "
        f"AND table_name IN ({tables})",
        tuple(importer.TABLES),
    ).fetchone()[0]


def _fingerprint(connection: psycopg.Connection[Any], table: str) -> tuple[int, str]:
    keys = (
        importer.TABLES[table][1]
        if table in importer.TABLES
        else adapter.AUXILIARY_TABLES[table][1]
    )
    ordering = ",".join(f't."{key}"' for key in keys)
    query = (
        f"SELECT count(*),md5(coalesce(string_agg(to_jsonb(t)::text,E'\\n' ORDER BY "
        f"{ordering}),'')) FROM public.\"{table}\" t"
    )
    count, digest = connection.execute(query).fetchone()
    return count, digest


def _prepare(
    connection: psycopg.Connection[Any], batch_id: str, *, incomplete: bool = False
) -> None:
    connection.execute(
        "DO $roles$ BEGIN CREATE ROLE anon NOLOGIN; EXCEPTION WHEN duplicate_object THEN NULL; END $roles$"
    )
    connection.execute(
        "DO $roles$ BEGIN CREATE ROLE authenticated NOLOGIN; EXCEPTION WHEN duplicate_object THEN NULL; END $roles$"
    )
    run_migrations(connection, postgres=True)
    connection.execute(
        "REVOKE ALL ON SCHEMA xbray_import_private FROM anon, authenticated"
    )
    connection.execute(
        "REVOKE ALL ON ALL TABLES IN SCHEMA xbray_import_private FROM anon, authenticated"
    )
    connection.execute(
        "INSERT INTO public.macro_score_snapshots "
        "(score_month,methodology_version,as_of,calculated_at,inputs_hash,payload_json,"
        "calculation_cycle_id,origin,created_at) VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s) "
        "ON CONFLICT DO NOTHING",
        (
            "2026-10",
            "v1",
            "2026-10-20",
            "2026-10T12:00:00+00:00",
            adapter.EXPECTED_INPUTS_HASH,
            "{}",
            adapter.EXPECTED_CYCLE,
            "production_refresh",
            "2026-10-20T12:00:00+00:00",
        ),
    )
    all_tables = tuple(importer.TABLES) + tuple(adapter.AUXILIARY_TABLES)
    baselines = {
        table: dict(
            zip(
                ("row_count", "fingerprint"),
                _fingerprint(connection, table),
                strict=True,
            )
        )
        for table in all_tables
    }
    baseline_json = {
        "baselines": baselines,
        "schema_fingerprint": _schema_fingerprint(connection),
    }
    manifest = {
        "bundle_sha256": batch_id,
        "row_counts": {
            table: 1 if table == "retrospective_macro_values" else 0
            for table in importer.TABLES
        },
    }
    counts = {
        table: 1 if table == "retrospective_macro_values" else 0
        for table in importer.TABLES
    }
    connection.execute(
        "INSERT INTO xbray_import_private.batches "
        "(batch_id,target_ref,transport_version,manifest_json,baseline_json,row_counts_json,state) "
        "VALUES (%s,%s,1,%s,%s,%s,'ready')",
        (
            batch_id,
            adapter.PROJECT_REF,
            json.dumps(manifest),
            json.dumps(baseline_json),
            json.dumps(counts),
        ),
    )
    if incomplete:
        return
    row = {
        "component_key": "adapter_test_component",
        "reference_month": "2026-10",
        "value": 0.1,
        "unit": "index",
        "formula_version": "test-v1",
        "source_basis": "retrospective_reference",
        "fetched_at": "2026-10-20T12:00:00+00:00",
        "inputs_json": "{}",
        "inputs_hash": "a" * 64,
        "reason": None,
    }
    payload = json.dumps(row, separators=(",", ":"))
    identity = importer.semantic_identity(
        "retrospective_macro_values",
        row,
        importer.TABLES["retrospective_macro_values"][1],
        {
            ("retrospective_macro_values", column): (
                "real" if column == "value" else "text"
            )
            for column in importer.TABLES["retrospective_macro_values"][0]
        },
    )
    digest = importer.semantic_row_hash(
        "retrospective_macro_values",
        row,
        {
            ("retrospective_macro_values", column): (
                "real" if column == "value" else "text"
            )
            for column in importer.TABLES["retrospective_macro_values"][0]
        },
    )
    connection.execute(
        "INSERT INTO xbray_import_private.rows "
        "(batch_id,table_name,chunk_index,ordinal,payload_json,payload_md5,row_identity_json,row_sha256,purpose) "
        "VALUES (%s,'retrospective_macro_values',0,0,%s::jsonb,md5((%s::jsonb)::text),%s,%s,'insert')",
        (batch_id, payload, payload, identity, digest),
    )
    connection.execute(
        "INSERT INTO xbray_import_private.chunks "
        "(batch_id,table_name,chunk_index,row_count,chunk_sha256,purpose) "
        "SELECT %s,'retrospective_macro_values',0,count(*),encode(sha256(convert_to("
        "jsonb_agg(jsonb_build_array(ordinal,payload_json,row_identity_json,row_sha256,purpose) "
        "ORDER BY ordinal)::text,'UTF8')),'hex'),'insert' FROM xbray_import_private.rows WHERE batch_id=%s",
        (batch_id, batch_id),
    )


def _execute_promotion(connection: psycopg.Connection[Any], batch_id: str) -> None:
    connection.execute(adapter._promotion_sql(batch_id))


def _remove_batch_rows(connection: psycopg.Connection[Any], batch_id: str) -> None:
    journal_rows = connection.execute(
        "SELECT table_name,row_identity_json FROM public.production_data_import_batch_rows "
        "WHERE batch_id=%s",
        (batch_id,),
    ).fetchall()
    for table, identity_text in journal_rows:
        identity = json.loads(identity_text)
        keys = importer.TABLES[table][1]
        where = sql.SQL(" AND ").join(
            sql.SQL("{}::text=%s").format(sql.Identifier(key)) for key in keys
        )
        values = tuple(str(identity[key]) for key in keys)
        connection.execute(
            sql.SQL("DELETE FROM public.{} WHERE ").format(sql.Identifier(table))
            + where,
            values,
        )
    connection.execute(
        "DELETE FROM public.production_data_import_batch_rows WHERE batch_id=%s",
        (batch_id,),
    )
    connection.execute(
        "DELETE FROM public.production_data_import_batches WHERE batch_id=%s",
        (batch_id,),
    )


def _local_transport(
    connection: psycopg.Connection[Any], sql: str, *, cli: str = "unused"
) -> dict[str, Any]:
    del cli
    cursor = connection.execute(sql)
    if cursor.description is None:
        return {"rows": []}
    names = [column.name for column in cursor.description]
    return {"rows": [dict(zip(names, row, strict=True)) for row in cursor.fetchall()]}


def _synthetic_bundle() -> tuple[dict[str, list[dict[str, object]]], dict[str, object]]:
    rows = {table: [] for table in importer.TABLES}
    inputs_json = "{}"
    rows["retrospective_macro_values"].append(
        {
            "component_key": "unemployment_rate",
            "reference_month": "2026-10",
            "value": 0.1,
            "unit": "index",
            "formula_version": "macro-retrospective-1.0.0",
            "source_basis": "retrospective_reference",
            "fetched_at": "2026-10-20T12:00:00+00:00",
            "inputs_json": inputs_json,
            "inputs_hash": hashlib.sha256(inputs_json.encode()).hexdigest(),
            "reason": None,
        }
    )
    manifest: dict[str, object] = {
        "manifest_version": "xbray-retrospective-production-import-v1",
        "source_files_sha256": {
            "di_ledger": "a" * 64,
            "non_di_macro_ledger": "b" * 64,
            "focus_vintages": "c" * 64,
        },
        "row_counts": {table: len(values) for table, values in rows.items()},
        "verified_di_distinct_months": {
            "juro_real_ex_ante_1y": 31,
            "inclinacao_di_5y_1y": 31,
        },
        "point_in_time": False,
        "total_rows": 1,
    }
    digest_rows = {
        table: sorted(values, key=importer.canonical_json)
        for table, values in rows.items()
    }
    manifest["bundle_sha256"] = hashlib.sha256(
        importer.canonical_json({"manifest": manifest, "rows": digest_rows}).encode()
    ).hexdigest()
    return rows, manifest


def _use_synthetic_bundle(monkeypatch: pytest.MonkeyPatch) -> dict[str, object]:
    rows, manifest = _synthetic_bundle()
    monkeypatch.setattr(importer, "source_bundle", lambda *_sources: (rows, manifest))
    monkeypatch.setattr(
        importer, "validate_bundle", lambda *_args: {"point_in_time": False}
    )
    return manifest


def test_pg17_transport_dryrun_reuses_importer_plan_and_never_writes(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    connection = _database()
    assert connection is not None
    try:
        connection.autocommit = True
        manifest = _use_synthetic_bundle(monkeypatch)
        setup_batch_id = hashlib.sha256(uuid4().bytes).hexdigest()
        _prepare(connection, setup_batch_id)
        connection.execute(
            "DELETE FROM xbray_import_private.batches WHERE batch_id=%s",
            (setup_batch_id,),
        )
        before = {table: _fingerprint(connection, table) for table in importer.TABLES}
        with patch.object(
            adapter,
            "_run_sql",
            side_effect=lambda sql, cli="unused": _local_transport(
                connection, sql, cli=cli
            ),
        ):
            result = adapter.run_supabase_import(
                di_db=Path("unused-di.db"),
                non_di_db=Path("unused-macro.db"),
                focus_db=Path("unused-focus.db"),
                target=importer.EXPECTED_TARGET,
                execute=False,
                confirm_target=None,
                expected_sha256=None,
                expected_total_rows=None,
                backup_path=None,
                repo_root=Path(__file__).resolve().parents[2],
            )
        assert result["bundle_sha256"] == manifest["bundle_sha256"]
        assert result["total_rows"] == 1
        assert result["point_in_time"] is False
        assert result["dry_run"] is True
        assert {
            table: _fingerprint(connection, table) for table in importer.TABLES
        } == before
        assert connection.execute(
            "SELECT count(*) FROM xbray_import_private.batches"
        ).fetchone() == (0,)
    finally:
        connection.execute(
            "DELETE FROM xbray_import_private.batches WHERE batch_id=%s",
            (setup_batch_id,),
        )
        connection.commit()
        connection.close()


def test_pg17_transport_execute_resume_idempotence_and_backup(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    connection = _database()
    assert connection is not None
    connection.autocommit = True
    manifest = _use_synthetic_bundle(monkeypatch)
    setup_batch_id = hashlib.sha256(uuid4().bytes).hexdigest()
    _prepare(connection, setup_batch_id)
    connection.execute(
        "DELETE FROM xbray_import_private.batches WHERE batch_id=%s", (setup_batch_id,)
    )
    monkeypatch.setenv(importer.IMPORT_TARGET_ENV, "xbray-api-production")
    calls = 0

    def bridge(sql: str, *, cli: str = "unused") -> dict[str, Any]:
        nonlocal calls
        calls += 1
        return _local_transport(connection, sql, cli=cli)

    try:
        original_bridge = bridge
        staging_writes = 0

        def interrupt_after_one_chunk(
            sql: str, *, cli: str = "unused"
        ) -> dict[str, Any]:
            nonlocal staging_writes
            if "INSERT INTO xbray_import_private.rows" in sql:
                staging_writes += 1
                result = original_bridge(sql, cli=cli)
                if staging_writes == 1:
                    raise RuntimeError("simulated staging disconnect")
                return result
            return original_bridge(sql, cli=cli)

        # Interrupt the actual staging transport after its first successful chunk.
        calls = 0
        with patch.object(adapter, "_run_sql", side_effect=interrupt_after_one_chunk):
            with pytest.raises(RuntimeError, match="simulated staging disconnect"):
                adapter.run_supabase_import(
                    di_db=Path("unused-di.db"),
                    non_di_db=Path("unused-macro.db"),
                    focus_db=Path("unused-focus.db"),
                    target=importer.EXPECTED_TARGET,
                    execute=True,
                    confirm_target=importer.EXPECTED_TARGET,
                    expected_sha256=manifest["bundle_sha256"],
                    expected_total_rows=manifest["total_rows"],
                    backup_path=tmp_path / "first-backup.gz",
                    repo_root=Path(__file__).resolve().parents[2],
                )
        assert (
            connection.execute(
                "SELECT count(*) FROM xbray_import_private.rows WHERE batch_id=%s",
                (manifest["bundle_sha256"],),
            ).fetchone()[0]
            > 0
        )
        with patch.object(adapter, "_run_sql", side_effect=bridge):
            result = adapter.run_supabase_import(
                di_db=Path("unused-di.db"),
                non_di_db=Path("unused-macro.db"),
                focus_db=Path("unused-focus.db"),
                target=importer.EXPECTED_TARGET,
                execute=True,
                confirm_target=importer.EXPECTED_TARGET,
                expected_sha256=manifest["bundle_sha256"],
                expected_total_rows=manifest["total_rows"],
                backup_path=tmp_path / "third-backup.gz",
                repo_root=Path(__file__).resolve().parents[2],
            )
        assert result["state"] == "committed"
        assert result["total_rows"] == 1
        with patch.object(adapter, "_run_sql", side_effect=bridge):
            repeated = adapter.run_supabase_import(
                di_db=Path("unused-di.db"),
                non_di_db=Path("unused-macro.db"),
                focus_db=Path("unused-focus.db"),
                target=importer.EXPECTED_TARGET,
                execute=True,
                confirm_target=importer.EXPECTED_TARGET,
                expected_sha256=manifest["bundle_sha256"],
                expected_total_rows=manifest["total_rows"],
                backup_path=tmp_path / "fourth-backup.gz",
                repo_root=Path(__file__).resolve().parents[2],
            )
        assert repeated["already_imported"] is True
        assert set(repeated["planned_inserts"].values()) == {0}
        assert (
            connection.execute(
                "SELECT count(*) FROM public.production_data_import_batch_rows WHERE batch_id=%s",
                (manifest["bundle_sha256"],),
            ).fetchone()[0]
            == manifest["total_rows"]
        )
    finally:
        _remove_batch_rows(connection, manifest["bundle_sha256"])
        connection.execute(
            "DELETE FROM xbray_import_private.batches WHERE batch_id=%s",
            (manifest["bundle_sha256"],),
        )
        connection.commit()
        connection.close()


def test_pg17_reuses_timestamp_only_vintage_without_journaling_or_mutating_it(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    connection = _database()
    assert connection is not None
    connection.autocommit = True
    rows, source_manifest = _synthetic_bundle()
    inputs = {
        "focus_12m_base0": {
            "payload_hash": hashlib.sha256(b'{"value":3.0}').hexdigest(),
            "available_at": "2026-10-20T02:37:31.818249+00:00",
            "value": 3.0,
        }
    }
    macro = rows["retrospective_macro_values"][0]
    macro["inputs_json"] = importer.canonical_json(inputs)
    macro["inputs_hash"] = hashlib.sha256(
        str(macro["inputs_json"]).encode()
    ).hexdigest()
    vintage = {
        "provider": "focus",
        "series_key": importer.FOCUS_KEY,
        "reference_period": "2026-10-16",
        "dimensions_json": "{}",
        "dimensions_hash": hashlib.sha256(b"{}").hexdigest(),
        "raw_value_json": '{"value":3.0}',
        "source_published_at": None,
        "first_seen_at": "2026-10-20T04:37:31.818249+02:00",
        "available_at": "2026-10-20T04:37:31.818249+02:00",
        "payload_hash": hashlib.sha256(b'{"value":3.0}').hexdigest(),
        "revision_id": hashlib.sha256(b'{"value":3.0}').hexdigest()[:16],
    }
    vintage_second = {**vintage, "reference_period": "2026-10-15"}
    inputs["focus_12m_base0"]["payload_hash"] = vintage["payload_hash"]
    macro["inputs_json"] = importer.canonical_json(inputs)
    macro["inputs_hash"] = hashlib.sha256(
        str(macro["inputs_json"]).encode()
    ).hexdigest()
    rows["observation_vintages"].append(vintage)
    rows["observation_vintages"].append(vintage_second)
    source_manifest["row_counts"] = {
        table: len(table_rows) for table, table_rows in rows.items()
    }
    source_manifest["total_rows"] = sum(len(table_rows) for table_rows in rows.values())
    source_manifest.pop("bundle_sha256")
    source_manifest["bundle_sha256"] = hashlib.sha256(
        importer.canonical_json(
            {
                "manifest": source_manifest,
                "rows": {
                    table: sorted(table_rows, key=importer.canonical_json)
                    for table, table_rows in rows.items()
                },
            }
        ).encode()
    ).hexdigest()
    setup_batch_id = hashlib.sha256(uuid4().bytes).hexdigest()
    batch_id: str | None = None
    target_first_seen = "2026-10-13T08:45:31.039291+00:00"
    try:
        _prepare(connection, setup_batch_id)
        connection.execute(
            "ALTER TABLE public.observation_vintages "
            "ALTER COLUMN reference_period TYPE date USING reference_period::date, "
            "ALTER COLUMN first_seen_at TYPE timestamptz USING first_seen_at::timestamptz, "
            "ALTER COLUMN available_at TYPE timestamptz USING available_at::timestamptz"
        )
        connection.execute(
            "DELETE FROM xbray_import_private.batches WHERE batch_id=%s",
            (setup_batch_id,),
        )
        for source_vintage in (vintage, vintage_second):
            target_vintage = {
                **source_vintage,
                "first_seen_at": target_first_seen,
                "available_at": target_first_seen,
            }
            columns = tuple(target_vintage)
            connection.execute(
                "INSERT INTO public.observation_vintages "
                f"({','.join(columns)}) VALUES ({','.join('%s' for _ in columns)})",
                tuple(target_vintage[column] for column in columns),
            )
        monkeypatch.setattr(
            importer,
            "source_bundle",
            lambda *_sources: (deepcopy(rows), deepcopy(source_manifest)),
        )
        monkeypatch.setattr(
            importer, "validate_bundle", lambda *_args: {"point_in_time": False}
        )

        def bridge(sql_text: str, *, cli: str = "unused") -> dict[str, Any]:
            return _local_transport(connection, sql_text, cli=cli)

        with patch.object(adapter, "_run_sql", side_effect=bridge):
            dry_run = adapter.run_supabase_import(
                di_db=Path("unused-di.db"),
                non_di_db=Path("unused-macro.db"),
                focus_db=Path("unused-focus.db"),
                target=importer.EXPECTED_TARGET,
                execute=False,
                confirm_target=None,
                expected_sha256=None,
                expected_total_rows=None,
                backup_path=None,
                repo_root=Path(__file__).resolve().parents[2],
                reuse_equivalent_vintages=True,
            )
        batch_id = str(dry_run["bundle_sha256"])
        assert dry_run["source_bundle_sha256"] == source_manifest["bundle_sha256"]
        assert dry_run["reused_equivalent_vintage_rows"] == 2
        assert dry_run["planned_insert_rows"] == 1
        assert dry_run["rebased_macro_dependency_rows"] == 0
        assert connection.execute(
            "SELECT first_seen_at,available_at FROM public.observation_vintages "
            "WHERE payload_hash=%s",
            (vintage["payload_hash"],),
        ).fetchone() == (
            datetime.fromisoformat(target_first_seen),
            datetime.fromisoformat(target_first_seen),
        )
        assert connection.execute(
            "SELECT count(*) FROM public.observation_vintages WHERE payload_hash=%s",
            (vintage["payload_hash"],),
        ).fetchone() == (2,)

        monkeypatch.setenv(importer.IMPORT_TARGET_ENV, "xbray-api-production")
        connection.execute(
            "UPDATE public.observation_vintages SET first_seen_at=%s "
            "WHERE payload_hash=%s",
            ("2026-10-14T08:45:31.039291+00:00", vintage["payload_hash"]),
        )
        stale_backup = tmp_path / "stale-equivalence-backup.json.gz"
        with patch.object(adapter, "_run_sql", side_effect=bridge):
            with pytest.raises(ValueError, match="exact source/resolved manifest"):
                adapter.run_supabase_import(
                    di_db=Path("unused-di.db"),
                    non_di_db=Path("unused-macro.db"),
                    focus_db=Path("unused-focus.db"),
                    target=importer.EXPECTED_TARGET,
                    execute=True,
                    confirm_target=importer.EXPECTED_TARGET,
                    expected_sha256=dry_run["bundle_sha256"],
                    expected_source_sha256=dry_run["source_bundle_sha256"],
                    expected_total_rows=dry_run["total_rows"],
                    expected_planned_rows=dry_run["planned_insert_rows"],
                    backup_path=stale_backup,
                    repo_root=Path(__file__).resolve().parents[2],
                    reuse_equivalent_vintages=True,
                )
        assert not stale_backup.exists()
        connection.execute(
            "UPDATE public.observation_vintages SET first_seen_at=%s "
            "WHERE payload_hash=%s",
            (target_first_seen, vintage["payload_hash"]),
        )
        with patch.object(adapter, "_run_sql", side_effect=bridge):
            dry_run = adapter.run_supabase_import(
                di_db=Path("unused-di.db"),
                non_di_db=Path("unused-macro.db"),
                focus_db=Path("unused-focus.db"),
                target=importer.EXPECTED_TARGET,
                execute=False,
                confirm_target=None,
                expected_sha256=None,
                expected_total_rows=None,
                backup_path=None,
                repo_root=Path(__file__).resolve().parents[2],
                reuse_equivalent_vintages=True,
            )
        batch_id = str(dry_run["bundle_sha256"])
        original_stage = adapter._insert_stage

        def stage_forged_payload(
            current_batch: str,
            table: str,
            chunk_index: int,
            records: list[dict[str, object]],
        ) -> None:
            original_stage(current_batch, table, chunk_index, records)
            if records and records[0].get("purpose") == "validation":
                connection.execute(
                    "UPDATE xbray_import_private.rows SET payload_json="
                    "jsonb_set(payload_json,'{raw_value_json}','\"forged\"'::jsonb), "
                    "payload_md5=md5((jsonb_set(payload_json,'{raw_value_json}',"
                    "'\"forged\"'::jsonb))::text) WHERE batch_id=%s AND purpose='validation'",
                    (current_batch,),
                )
                connection.execute(
                    "UPDATE xbray_import_private.chunks c SET chunk_sha256="
                    "encode(sha256(convert_to((SELECT jsonb_agg(jsonb_build_array("
                    "ordinal,payload_json,row_identity_json,row_sha256,purpose) ORDER BY ordinal)::text "
                    "FROM xbray_import_private.rows r WHERE r.batch_id=c.batch_id "
                    "AND r.table_name=c.table_name AND r.chunk_index=c.chunk_index),'UTF8')),'hex') "
                    "WHERE c.batch_id=%s AND c.purpose='validation'",
                    (current_batch,),
                )

        monkeypatch.setenv(importer.IMPORT_TARGET_ENV, "xbray-api-production")
        with (
            patch.object(adapter, "_run_sql", side_effect=bridge),
            patch.object(adapter, "_insert_stage", side_effect=stage_forged_payload),
        ):
            with pytest.raises(
                psycopg.Error, match="production vintage does not match"
            ):
                adapter.run_supabase_import(
                    di_db=Path("unused-di.db"),
                    non_di_db=Path("unused-macro.db"),
                    focus_db=Path("unused-focus.db"),
                    target=importer.EXPECTED_TARGET,
                    execute=True,
                    confirm_target=importer.EXPECTED_TARGET,
                    expected_sha256=dry_run["bundle_sha256"],
                    expected_source_sha256=dry_run["source_bundle_sha256"],
                    expected_total_rows=dry_run["total_rows"],
                    expected_planned_rows=dry_run["planned_insert_rows"],
                    backup_path=tmp_path / "tampered-validation-backup.json.gz",
                    repo_root=Path(__file__).resolve().parents[2],
                    reuse_equivalent_vintages=True,
                )
        connection.rollback()
        assert connection.execute(
            "SELECT count(*) FROM public.retrospective_macro_values WHERE component_key='unemployment_rate'"
        ).fetchone() == (0,)
        assert connection.execute(
            "SELECT count(*) FROM public.production_data_import_batches WHERE batch_id=%s",
            (batch_id,),
        ).fetchone() == (0,)
        with patch.object(adapter, "_run_sql", side_effect=bridge):
            adapter.discard_staging_batch(batch_id)
        with patch.object(adapter, "_run_sql", side_effect=bridge):
            result = adapter.run_supabase_import(
                di_db=Path("unused-di.db"),
                non_di_db=Path("unused-macro.db"),
                focus_db=Path("unused-focus.db"),
                target=importer.EXPECTED_TARGET,
                execute=True,
                confirm_target=importer.EXPECTED_TARGET,
                expected_sha256=dry_run["bundle_sha256"],
                expected_source_sha256=dry_run["source_bundle_sha256"],
                expected_total_rows=dry_run["total_rows"],
                expected_planned_rows=dry_run["planned_insert_rows"],
                backup_path=tmp_path / "equivalent-vintage-backup.json.gz",
                repo_root=Path(__file__).resolve().parents[2],
                reuse_equivalent_vintages=True,
            )
        assert result["state"] == "committed"
        assert connection.execute(
            "SELECT table_name FROM public.production_data_import_batch_rows WHERE batch_id=%s",
            (batch_id,),
        ).fetchall() == [("retrospective_macro_values",)]
        assert connection.execute(
            "SELECT first_seen_at,available_at FROM public.observation_vintages "
            "WHERE payload_hash=%s",
            (vintage["payload_hash"],),
        ).fetchone() == (
            datetime.fromisoformat(target_first_seen),
            datetime.fromisoformat(target_first_seen),
        )
        with patch.object(adapter, "_run_sql", side_effect=bridge):
            repeated = adapter.run_supabase_import(
                di_db=Path("unused-di.db"),
                non_di_db=Path("unused-macro.db"),
                focus_db=Path("unused-focus.db"),
                target=importer.EXPECTED_TARGET,
                execute=False,
                confirm_target=None,
                expected_sha256=None,
                expected_total_rows=None,
                backup_path=None,
                repo_root=Path(__file__).resolve().parents[2],
                reuse_equivalent_vintages=True,
            )
        assert repeated["bundle_sha256"] == batch_id
        assert repeated["already_imported"] is True
        assert repeated["planned_inserts"]["observation_vintages"] == 0
        connection.execute(
            "UPDATE public.observation_vintages SET available_at=%s "
            "WHERE payload_hash=%s",
            ("2026-10-14T08:45:31.039291+00:00", vintage["payload_hash"]),
        )
        with patch.object(adapter, "_run_sql", side_effect=bridge):
            changed_baseline_repeat = adapter.run_supabase_import(
                di_db=Path("unused-di.db"),
                non_di_db=Path("unused-macro.db"),
                focus_db=Path("unused-focus.db"),
                target=importer.EXPECTED_TARGET,
                execute=False,
                confirm_target=None,
                expected_sha256=None,
                expected_total_rows=None,
                backup_path=None,
                repo_root=Path(__file__).resolve().parents[2],
                reuse_equivalent_vintages=True,
            )
        assert changed_baseline_repeat["already_imported"] is True
        assert set(changed_baseline_repeat["planned_inserts"].values()) == {0}
    finally:
        if batch_id:
            _remove_batch_rows(connection, batch_id)
            connection.execute(
                "DELETE FROM xbray_import_private.batches WHERE batch_id=%s",
                (batch_id,),
            )
        connection.execute(
            "DELETE FROM xbray_import_private.batches WHERE batch_id=%s",
            (setup_batch_id,),
        )
        connection.execute(
            "DELETE FROM public.observation_vintages WHERE payload_hash=%s",
            (vintage["payload_hash"],),
        )
        connection.execute(
            "ALTER TABLE public.observation_vintages "
            "ALTER COLUMN reference_period TYPE text USING reference_period::text, "
            "ALTER COLUMN first_seen_at TYPE text USING first_seen_at::text, "
            "ALTER COLUMN available_at TYPE text USING available_at::text"
        )
        connection.commit()
        connection.close()


def test_pg17_atomic_promotion_idempotence_and_role_privacy() -> None:
    connection = _database()
    assert connection is not None
    batch_id = hashlib.sha256(uuid4().bytes).hexdigest()
    try:
        connection.autocommit = True
        _prepare(connection, batch_id)
        _execute_promotion(connection, batch_id)
        assert connection.execute(
            "SELECT value::text FROM public.retrospective_macro_values WHERE component_key='adapter_test_component'"
        ).fetchone() == ("0.1",)
        assert connection.execute(
            "SELECT state FROM public.production_data_import_batches WHERE batch_id=%s",
            (batch_id,),
        ).fetchone() == ("committed",)
        with pytest.raises(psycopg.Error):
            _execute_promotion(connection, batch_id)
        connection.rollback()
        assert connection.execute(
            "SELECT count(*) FROM public.retrospective_macro_values WHERE component_key='adapter_test_component'"
        ).fetchone() == (1,)
        privileges = connection.execute(
            "SELECT has_schema_privilege('anon','xbray_import_private','USAGE'), "
            "has_table_privilege('anon','xbray_import_private.rows','SELECT'), "
            "has_schema_privilege('authenticated','xbray_import_private','USAGE'), "
            "has_table_privilege('authenticated','xbray_import_private.rows','SELECT')"
        ).fetchone()
        assert privileges == (False, False, False, False)
    finally:
        connection.execute(
            "DELETE FROM public.production_data_import_batches WHERE batch_id=%s",
            (batch_id,),
        )
        connection.execute(
            "DELETE FROM public.retrospective_macro_values WHERE component_key='adapter_test_component'"
        )
        connection.execute(
            "DELETE FROM xbray_import_private.batches WHERE batch_id=%s", (batch_id,)
        )
        connection.commit()
        connection.close()


def test_pg17_midpromotion_failure_rolls_back_ledger_and_journal() -> None:
    connection = _database()
    assert connection is not None
    batch_id = hashlib.sha256(uuid4().bytes).hexdigest()
    try:
        connection.autocommit = True
        _prepare(connection, batch_id)
        connection.execute(
            "CREATE OR REPLACE FUNCTION public.xbray_test_abort_import() RETURNS trigger "
            "LANGUAGE plpgsql AS $$ BEGIN IF NEW.component_key='adapter_test_component' THEN "
            "RAISE EXCEPTION 'injected promotion failure'; END IF; RETURN NEW; END $$"
        )
        connection.execute(
            "CREATE TRIGGER xbray_test_abort_import BEFORE INSERT ON public.retrospective_macro_values "
            "FOR EACH ROW EXECUTE FUNCTION public.xbray_test_abort_import()"
        )
        before = {table: _fingerprint(connection, table) for table in importer.TABLES}
        with pytest.raises(psycopg.Error, match="injected promotion failure"):
            _execute_promotion(connection, batch_id)
        connection.rollback()
        assert {
            table: _fingerprint(connection, table) for table in importer.TABLES
        } == before
        assert connection.execute(
            "SELECT count(*) FROM public.production_data_import_batches WHERE batch_id=%s",
            (batch_id,),
        ).fetchone() == (0,)
    finally:
        connection.execute(
            "DROP TRIGGER IF EXISTS xbray_test_abort_import ON public.retrospective_macro_values"
        )
        connection.execute("DROP FUNCTION IF EXISTS public.xbray_test_abort_import()")
        connection.execute(
            "DELETE FROM public.retrospective_macro_values WHERE component_key='adapter_test_component'"
        )
        connection.execute(
            "DELETE FROM xbray_import_private.batches WHERE batch_id=%s", (batch_id,)
        )
        connection.commit()
        connection.close()


@pytest.mark.parametrize(
    "failure", ["incomplete", "stale_baseline", "wrong_target", "bad_hash"]
)
def test_pg17_promotion_failure_is_atomic(failure: str) -> None:
    connection = _database()
    assert connection is not None
    batch_id = hashlib.sha256((failure + uuid4().hex).encode()).hexdigest()
    try:
        connection.autocommit = True
        _prepare(connection, batch_id, incomplete=failure == "incomplete")
        if failure == "stale_baseline":
            connection.execute(
                "INSERT INTO public.retrospective_macro_values "
                "VALUES ('concurrent_test_component','2026-10',0.2,'index','v1',"
                "'retrospective_reference','2026-10-20T12:00:00+00:00','{}',%s,NULL)",
                ("b" * 64,),
            )
        elif failure == "wrong_target":
            connection.execute(
                "UPDATE xbray_import_private.batches SET target_ref='wrong' WHERE batch_id=%s",
                (batch_id,),
            )
        elif failure == "bad_hash":
            connection.execute(
                "UPDATE xbray_import_private.rows SET row_sha256=%s WHERE batch_id=%s",
                ("0" * 64, batch_id),
            )
        before = {table: _fingerprint(connection, table) for table in importer.TABLES}
        with pytest.raises(psycopg.Error):
            _execute_promotion(connection, batch_id)
        connection.rollback()
        after = {table: _fingerprint(connection, table) for table in importer.TABLES}
        assert after == before
        assert connection.execute(
            "SELECT count(*) FROM public.production_data_import_batches WHERE batch_id=%s",
            (batch_id,),
        ).fetchone() == (0,)
    finally:
        connection.execute(
            "DELETE FROM public.retrospective_macro_values WHERE component_key='concurrent_test_component'"
        )
        connection.execute(
            "DELETE FROM public.retrospective_macro_values WHERE component_key='adapter_test_component'"
        )
        connection.execute(
            "DELETE FROM xbray_import_private.batches WHERE batch_id=%s", (batch_id,)
        )
        connection.commit()
        connection.close()
