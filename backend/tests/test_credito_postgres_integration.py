from __future__ import annotations

import hashlib
import json
import os
import re
from collections.abc import Iterator
from datetime import date, datetime, timedelta, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from threading import Thread, local
from typing import ClassVar
from unittest.mock import patch
from urllib.parse import parse_qs, urlparse
from uuid import uuid4

import psycopg
import pytest
from fastapi.testclient import TestClient
from psycopg.conninfo import conninfo_to_dict, make_conninfo

from db import store

pytestmark = pytest.mark.skipif(
    not os.environ.get("TEST_DATABASE_URL"),
    reason="requires disposable local PostgreSQL 17 TEST_DATABASE_URL",
)

@pytest.fixture
def isolated_credit_pg17(monkeypatch: pytest.MonkeyPatch) -> Iterator[str]:
    dsn = os.environ["TEST_DATABASE_URL"]
    options = conninfo_to_dict(dsn)
    if options.get("host") not in {"127.0.0.1", "localhost", "::1"}:
        pytest.fail("credit integration requires loopback-only PostgreSQL")
    if options.get("dbname") != "xbray_test":
        pytest.fail("credit integration requires the isolated xbray_test database")
    with psycopg.connect(dsn, autocommit=True) as admin:
        version, database = admin.execute(
            "SELECT current_setting('server_version_num')::int, current_database()"
        ).fetchone()
        if version < 170000 or version >= 180000 or database != "xbray_test":
            pytest.fail("credit integration requires PostgreSQL 17 in xbray_test")
        schema = f"credit_pf_{uuid4().hex}"
        admin.execute(f'CREATE SCHEMA "{schema}"')
    if store._conn is not None:
        store._conn.close()
    store._conn = None
    prior = getattr(store._pg_local, "conn", None)
    if prior is not None:
        prior.close()
    monkeypatch.setattr(store, "DATABASE_URL", make_conninfo(dsn, options=f"-c search_path={schema}"))
    monkeypatch.setattr(store, "_pg_local", local())
    store.init_db()
    try:
        yield dsn
    finally:
        connection = getattr(store._pg_local, "conn", None)
        if connection is not None:
            connection.close()
        with psycopg.connect(dsn, autocommit=True) as admin:
            admin.execute(f'DROP SCHEMA "{schema}" CASCADE')
        monkeypatch.setattr(store, "_pg_local", local())


def _synthetic_monthly_rows(code: int) -> list[dict[str, str]]:
    rows: list[dict[str, str]] = []
    for index in range(15):
        year, month = 2025 + index // 12, index % 12 + 1
        if month == 8 and year == 2025:
            continue
        value = float(100 + index * 2 + code % 10)
        if code == 20573:
            value = {
                (2025, 1): 100.5,
                (2025, 3): 105.0,
                (2025, 12): 110.5,
                (2026, 1): 120.5,
                (2026, 2): 118.0,
                (2026, 3): 120.5,
            }.get((year, month), value)
        if code == 20587 and (year, month) == (2025, 3):
            value = 0.0
        rows.append({"data": date(year, month, 1).strftime("%d/%m/%Y"), "valor": str(value)})
    return rows


def _parse_fixture_date(raw: str) -> date:
    day, month, year = (int(part) for part in raw.split("/"))
    return date(year, month, day)


def _database_fingerprint(connection: psycopg.Connection[object], schema: str) -> str:
    table_names = (
        "sgs", "observation_vintages", "source_observation_origins",
        "source_extractions", "source_acquisition_attempts", "source_acquisition_commits",
        "sgs_coverage_receipts", "sgs_unit_commits", "sgs_refresh_plans",
        "sgs_refresh_plan_units", "refresh_runs", "refresh_work_items",
        "derived_snapshots", "macro_score_snapshots",
    )
    snapshot: list[tuple[str, tuple[tuple[object, ...], ...]]] = []
    for table in table_names:
        rows = connection.execute(f'SELECT * FROM "{schema}"."{table}"').fetchall()
        snapshot.append((table, tuple(sorted(rows, key=repr))))
    encoded = json.dumps(snapshot, default=str, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(encoded.encode()).hexdigest()


def test_native_pg17_raw_sgs_pipeline_and_credit_api_quality_are_consistent(
    isolated_credit_pg17: str,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import provider_http
    from config import CREDIT
    from datafetchers import bcb_sgs
    from db.refresh_attempt import (
        begin_refresh_attempt,
        install_refresh_attempt,
        new_refresh_attempt,
        reset_refresh_attempt,
    )
    from db.refresh_run_models import (
        CycleId,
        RefreshRunStart,
        RefreshWorkItemClaimRequest,
        RefreshWorkItemCompletion,
        RefreshWorkItemOutcome,
        RefreshWorkItemSpec,
        RunId,
        WorkItemKey,
    )
    from db.refresh_runs import (
        claim_next_work_item,
        complete_work_item_claim,
        create_or_resume_run,
    )
    from db.sgs_refresh_plan import (
        active_sgs_series_codes,
        build_sgs_plan,
        freeze_sgs_plan,
        plan_item_task_name,
        read_sgs_plan,
    )
    from main import app
    from tests.test_credito import _contract

    dsn = isolated_credit_pg17
    capture = datetime.now(timezone.utc)
    plan = build_sgs_plan(capture.date(), max_archive_units=4)
    recent_units = [unit for unit in plan.units if unit.role == "recent"]
    archive_units = [unit for unit in plan.units if unit.role != "recent"]
    assert len(recent_units) == len(active_sgs_series_codes())
    assert len(archive_units) == 4
    assert len(plan.units) == 68 + 4
    assert len(plan.units) + 6 + 3 == 81
    target_codes = {20573, 20574, 20587, 20588}
    target_units = [unit for unit in recent_units if unit.series_code in target_codes]
    assert {unit.series_code for unit in target_units} == target_codes

    plan_run_id = RunId(f"credit-pg-{uuid4().hex}")
    work_items = tuple(
        RefreshWorkItemSpec(WorkItemKey(plan_item_task_name(unit)), "bcb_sgs", order)
        for order, unit in enumerate(target_units, start=1)
    )
    create_or_resume_run(RefreshRunStart(
        run_id=plan_run_id,
        cycle_id=CycleId(str(plan_run_id)),
        force=True,
        manifest_version="daily-refresh-v6",
        derived_cutoff=None,
        macro_cutoff=None,
        items=work_items,
        now=capture.isoformat(),
        lease_expires_at=(capture + timedelta(minutes=30)).isoformat(),
    ))
    freeze_sgs_plan(str(plan_run_id), plan, capture)

    for name, records in _contract()["data"].items():
        store.upsert_sgs(int(CREDIT[name]), records, first_seen_at=capture.isoformat())

    synthetic_by_code = {code: _synthetic_monthly_rows(code) for code in sorted(target_codes)}

    class FixtureHandler(BaseHTTPRequestHandler):
        seen: ClassVar[list[tuple[int, str | None, str | None]]] = []

        def do_GET(self) -> None:
            match = re.search(r"bcdata\.sgs\.(\d+)/dados", self.path)
            if match is None:
                self.send_error(404)
                return
            code = int(match.group(1))
            query = parse_qs(urlparse(self.path).query)
            start_text = query.get("dataInicial", [None])[0]
            end_text = query.get("dataFinal", [None])[0]
            type(self).seen.append((code, start_text, end_text))
            start = _parse_fixture_date(start_text) if start_text else date.min
            end = _parse_fixture_date(end_text) if end_text else date.max
            records = [
                row for row in synthetic_by_code[code]
                if start <= _parse_fixture_date(row["data"]) <= end
            ]
            body = json.dumps(records, separators=(",", ":")).encode()
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, _format: str, *_args: object) -> None:
            return

    FixtureHandler.seen = []
    server = ThreadingHTTPServer(("127.0.0.1", 0), FixtureHandler)
    server.daemon_threads = True
    server_thread = Thread(target=server.serve_forever, daemon=True)
    server_thread.start()
    monkeypatch.setattr(
        bcb_sgs,
        "BCB_SGS_BASE",
        f"http://127.0.0.1:{server.server_port}/dados/serie/bcdata.sgs.{{code}}/dados",
    )
    monkeypatch.setattr(provider_http, "require_allowed_provider_url", lambda url: url)

    try:
        for unit in target_units:
            item_key = WorkItemKey(plan_item_task_name(unit))
            claim = claim_next_work_item(RefreshWorkItemClaimRequest(
                plan_run_id, capture.isoformat(),
                (capture + timedelta(minutes=20)).isoformat(), item_key,
            ))
            assert claim is not None
            attempt = new_refresh_attempt(claim)
            begin_refresh_attempt(attempt, capture.isoformat())
            store.mark_refresh_acquisition_started(attempt, capture.isoformat())
            attempt_token = install_refresh_attempt(attempt)
            try:
                records = bcb_sgs.fetch_sgs_series(
                    unit.series_code,
                    start_date=unit.start_date.strftime("%d/%m/%Y"),
                    end_date=unit.end_date.strftime("%d/%m/%Y"),
                    use_cache=False,
                    mark_refreshed=False,
                    fail_fast=True,
                )
            finally:
                reset_refresh_attempt(attempt_token)
            assert len(records) == 14
            assert complete_work_item_claim(RefreshWorkItemCompletion(
                run_id=plan_run_id,
                item_key=item_key,
                claim_token=claim.claim_token,
                status=RefreshWorkItemOutcome.SUCCEEDED,
                finished_at=datetime.now(timezone.utc).isoformat(),
                series_updated=1,
                record_count=len(records),
                error_code=None,
            ))
    finally:
        server.shutdown()
        server.server_close()
        server_thread.join(timeout=3)

    assert len(FixtureHandler.seen) >= len(target_units)
    assert {code for code, _start, _end in FixtureHandler.seen} == target_codes
    assert all(start and end for _code, start, end in FixtureHandler.seen)
    assert {row["status"] for row in store.latest_source_extractions(20)
            if row["indicator"] in {str(code) for code in target_codes}} == {"success"}
    assert len(store._query(
        "SELECT * FROM sgs_unit_commits WHERE run_id = ?", (str(plan_run_id),)
    )) == 4
    # Monthly recent units carry validated unit commits; archive coverage receipts
    # are intentionally not created for recent-window acquisitions.
    assert len(store._query(
        "SELECT * FROM sgs_coverage_receipts WHERE run_id = ?", (str(plan_run_id),)
    )) == 0
    assert len(store._query(
        "SELECT * FROM observation_vintages WHERE provider = 'bcb_sgs' "
        "AND series_key IN ('carteira_pf_cheque_especial', "
        "'carteira_pf_credito_pessoal_nao_consignado', "
        "'carteira_pf_cartao_rotativo', 'carteira_pf_cartao_parcelado')"
    )) == 56
    assert read_sgs_plan(str(plan_run_id)) == plan

    schema = store._query("SELECT current_schema() AS schema")[0]["schema"]
    with psycopg.connect(dsn) as connection:
        before = _database_fingerprint(connection, schema)
    source_values = {code: store.query_sgs(code) for code in sorted(target_codes)}
    expected_legacy = _contract()
    class AsOfDatetime(datetime):
        @classmethod
        def now(cls, tz=None):
            return datetime(2026, 9, 21, tzinfo=timezone.utc)

    monkeypatch.setattr("routes.credito.datetime", AsOfDatetime)
    with (
        patch("provider_http.get_provider_response", side_effect=AssertionError("GET attempted network")),
        patch("datafetchers.bcb_sgs._fetch_sgs_json", side_effect=AssertionError("GET attempted raw HTTP")),
        patch.object(store, "_execute", side_effect=AssertionError("GET attempted SQL write")),
        patch.object(store, "execute_statement", side_effect=AssertionError("GET attempted SQL write")),
        patch.object(store, "execute_transaction", side_effect=AssertionError("GET attempted SQL write")),
        TestClient(app) as client,
    ):
        response = client.get("/api/credito")
        quality_response = client.get("/api/data-quality/credito")
    assert response.status_code == 200
    body = response.json()
    export_path = os.environ.get("XBRAY_CREDIT_CONTRACT_EXPORT")
    if export_path:
        destination = Path(export_path)
        destination.parent.mkdir(parents=True, exist_ok=True)
        destination.write_text(json.dumps(body, ensure_ascii=False), encoding="utf-8")
    assert body["status"] == "ok"
    assert quality_response.status_code == 200
    quality = quality_response.json()
    assert quality["status"] == body["status"]
    assert quality["unavailable"] == []
    assert len(quality["metadata"]) == 9
    assert quality["last_updated"] == "2026-09-01"
    for name, records in expected_legacy["data"].items():
        assert body["data"][name] == records
        assert body["metadata"][name] == expected_legacy["metadata"][name]
    assert {body["modalities"][key]["code"] for key in body["modalities"]} == target_codes
    assert all(body["modalities"][key]["unit"] == "brl_million" for key in body["modalities"])
    assert body["modalities"]["carteira_pf_cheque_especial"]["latest"] == {
        "reference": "2026-03-01", "value": 120.5, "reason": None,
    }
    cheque = body["modalities"]["carteira_pf_cheque_especial"]
    assert cheque["mom"]["value_pct"] == pytest.approx((120.5 / 118 - 1) * 100)
    assert cheque["mom"]["base_reference"] == "2026-02-01"
    assert cheque["yoy"]["value_pct"] == pytest.approx((120.5 / 105 - 1) * 100)
    assert cheque["yoy"]["base_reference"] == "2025-03-01"
    assert next(point for point in cheque["history"] if point["reference"] == "2025-08-01")["value"] is None
    rotativo = body["modalities"]["carteira_pf_cartao_rotativo"]
    march_point = next(point for point in rotativo["history"] if point["reference"] == "2026-03-01")
    assert march_point["yoy_pct"] is None
    assert march_point["yoy_base_reference"] == "2025-03-01"
    assert march_point["yoy_reason"] == "zero_base"
    assert all(source_values[code] for code in target_codes)
    with psycopg.connect(dsn) as connection:
        assert _database_fingerprint(connection, schema) == before
