from __future__ import annotations

import json
import os
from collections.abc import Iterator
from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
from datetime import date, datetime, timedelta, timezone
from email.utils import format_datetime
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from threading import Barrier, Event, Thread, local
from time import monotonic
from unittest.mock import MagicMock, patch
from urllib.parse import parse_qs, urlparse
from uuid import uuid4

import psycopg
import pytest
from psycopg.conninfo import conninfo_to_dict, make_conninfo
from psycopg.errors import RaiseException

from datafetchers import b3_di, bcb_sgs, focus, tesouro_direto
from db import store
from ingestion_policy import (
    RetryBudget,
    current_retry_budget,
    install_retry_budget,
    reset_retry_budget,
    retry_profile,
)
from refresh import RefreshTask, run_refresh_task_detailed
from source_ingestion import FocusIngestion, reset_refresh_context, set_refresh_context


@pytest.fixture
def isolated_pg17_schema(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    database_url = os.environ.get("TEST_DATABASE_URL")
    if not database_url:
        pytest.skip("requires disposable PostgreSQL 17 TEST_DATABASE_URL")
    parsed = conninfo_to_dict(database_url)
    if not parsed.get("dbname", "").endswith("_test"):
        pytest.fail("TEST_DATABASE_URL must point to an isolated *_test database")
    with psycopg.connect(database_url, autocommit=True) as admin, admin.cursor() as cursor:
        cursor.execute("SELECT current_setting('server_version_num')::int, current_database()")
        version, database_name = cursor.fetchone()
        if version < 170000 or version >= 180000:
            pytest.fail("integration fixture requires PostgreSQL 17")
        if database_name != parsed.get("dbname"):
            pytest.fail("integration fixture database identity changed during connection")
        schema = f"xbray_source_ingestion_{uuid4().hex}"
        cursor.execute(f'CREATE SCHEMA "{schema}"')
    monkeypatch.setattr(store, "DATABASE_URL", make_conninfo(database_url, options=f"-c search_path={schema}"))
    monkeypatch.setattr(store, "_pg_local", local())
    store.init_db()
    try:
        yield
    finally:
        connection = getattr(store._pg_local, "conn", None)
        if connection is not None:
            connection.close()
        with psycopg.connect(database_url, autocommit=True) as admin, admin.cursor() as cursor:
            cursor.execute(f'DROP SCHEMA "{schema}" CASCADE')
        monkeypatch.setattr(store, "_pg_local", local())


def test_pg17_migration_registry_focus_per_family_and_ledger_lifecycle(
    isolated_pg17_schema: None,
) -> None:
    versions = store._query("SELECT version FROM schema_migrations ORDER BY version")
    assert versions[-1]["version"] == "021_treasury_intent_trace"
    columns = store._query(
        "SELECT column_name FROM information_schema.columns "
        "WHERE table_name = 'refresh_work_items'"
    )
    assert {
        "claim_generation", "acquisition_attempts", "next_attempt_at", "deferral_reason",
        "last_error_category", "last_error_code",
    } <= {
        row["column_name"] for row in columns
    }
    with patch("datafetchers.focus.get_provider_response") as provider:
        def response(*_args: object, **kwargs: object) -> MagicMock:
            provider_indicator = kwargs["params"]["$filter"].split("'")[1]
            indicator = "PIB" if provider_indicator == "PIB Total" else provider_indicator
            now = datetime.now(timezone.utc).date().isoformat()
            records = [
                {
                    "Data": now,
                    "DataReferencia": target_year,
                    "Indicador": provider_indicator,
                    "baseCalculo": base,
                    "Suavizada": None,
                    "Mediana": 4.0 + base,
                }
                for target_year in range(datetime.now(timezone.utc).year, datetime.now(timezone.utc).year + 5)
                for base in (0, 1)
            ]
            # Keep per-target identity in records; the local variable verifies
            # that the adapter preserves canonical versus provider naming.
            assert indicator in focus.INDICATORS
            result = MagicMock()
            result.json.return_value = {"value": records}
            return result

        provider.side_effect = response
        store.set_meta("focus_last_refresh", datetime.now(timezone.utc).isoformat())
        refreshed = focus.fetch_all_focus(use_cache=True)

    assert provider.call_count == len(focus.INDICATORS)
    assert all(isinstance(refreshed[name], list) and len(refreshed[name]) == 10 for name in focus.INDICATORS)
    rows = store._query(
        "SELECT source, indicator, status, network_called, source_data_date, valid_count, "
        "persisted_count, run_id FROM source_extractions ORDER BY indicator"
    )
    assert len(rows) == len(focus.INDICATORS)
    assert {row["source"] for row in rows} == {"focus"}
    assert {row["status"] for row in rows} == {"success"}
    assert {row["network_called"] for row in rows} == {1}
    assert {row["valid_count"] for row in rows} == {10}
    assert {row["persisted_count"] for row in rows} == {10}
    assert all(row["source_data_date"] is not None for row in rows)

    with patch("datafetchers.focus.get_provider_response") as provider:
        cached = focus.fetch_all_focus(use_cache=True)
    provider.assert_not_called()
    assert all(isinstance(cached[name], list) and len(cached[name]) == 10 for name in focus.INDICATORS)

    token = set_refresh_context("pg-run-1", "focus:Selic", "cycle-1")
    try:
        with pytest.raises(TimeoutError):
            FocusIngestion().fetch(
                indicator="Selic", qualifiers={"kind": "annual"},
                operation=lambda: (_ for _ in ()).throw(TimeoutError("provider unavailable")),
            )
    finally:
        reset_refresh_context(token)

    attempts = store._query(
        "SELECT status, network_called, extracted_at, run_id, task_key, cycle_id "
        "FROM source_extractions WHERE indicator = 'Selic' ORDER BY started_at, extraction_id"
    )
    assert len(attempts) == 3
    assert [(row["status"], row["network_called"]) for row in attempts] == [
        ("success", 1), ("cache_hit", 0), ("network_error", 1),
    ]
    assert attempts[1]["extracted_at"] is None
    assert attempts[2]["run_id"] == "pg-run-1"
    assert attempts[2]["task_key"] == "focus:Selic"
    assert attempts[2]["cycle_id"] == "cycle-1"

    def family_response(*_args: object, **kwargs: object) -> MagicMock:
        provider_indicator = kwargs["params"]["$filter"].split("'")[1]
        if provider_indicator == "IPCA":
            raise TimeoutError("provider timeout")
        payload = MagicMock()
        payload.json.return_value = {"value": [{
            "Data": datetime.now(timezone.utc).date().isoformat(),
            "DataReferencia": datetime.now(timezone.utc).year,
            "Indicador": provider_indicator,
            "baseCalculo": 0,
            "Suavizada": "S",
            "Mediana": 4.2,
        }]}
        return payload

    with patch("datafetchers.focus.get_provider_response", side_effect=family_response):
        task_result = run_refresh_task_detailed(RefreshTask(
            "focus", "focus", "olinda.bcb.gov.br", len(focus.INDICATORS),
            lambda: focus.fetch_all_focus(use_cache=False),
        ))
    assert task_result.result["status"] == "partial"
    assert task_result.result["error_code"] == "PartialSourceFailures"
    assert task_result.series_updated == len(focus.INDICATORS) - 1
    assert task_result.record_count == len(focus.INDICATORS) - 1
    focus_rows = store._query(
        "SELECT indicator, status, network_called, received_count, valid_count, persisted_count "
        "FROM source_extractions WHERE strpos(qualifiers_json, 'annual') > 0 "
        "ORDER BY started_at DESC, indicator"
    )
    latest: dict[str, dict[str, object]] = {}
    for row in focus_rows:
        latest.setdefault(row["indicator"], row)
    assert set(latest) == set(focus.INDICATORS)
    assert latest["IPCA"]["status"] == "network_error"
    assert latest["IPCA"]["network_called"] == 1
    assert all(latest[ind]["status"] == "success" for ind in focus.INDICATORS if ind != "IPCA")
    assert all(latest[ind]["persisted_count"] == 1 for ind in focus.INDICATORS if ind != "IPCA")

    with patch("datafetchers.focus.get_provider_response", side_effect=TimeoutError("offline")):
        all_failed = run_refresh_task_detailed(RefreshTask(
            "focus", "focus", "olinda.bcb.gov.br", len(focus.INDICATORS),
            lambda: focus.fetch_all_focus(use_cache=False),
        ))
    assert all_failed.result["status"] == "error"
    assert all_failed.series_updated == all_failed.record_count == 0


def test_pg17_refresh_extraction_query_binds_like_patterns(
    isolated_pg17_schema: None,
) -> None:
    run_id = f"pg-like-{uuid4().hex}"
    fixtures = [
        ("sgs_juros_w226_2025", run_id, "partial", '{"identity_kind":"full_period"}', 4),
        ("sgs_inflacao_w433_2025", run_id, "success", '{"unit":"pct_period"}', 3),
        ("other_task", run_id, "success", "{}", 1),
        ("sgs_juros_w226_2025", f"other-{run_id}", "success", "{}", 8),
    ]
    for index, (task, row_run_id, status, qualifiers, count) in enumerate(fixtures):
        store._execute(
            "INSERT INTO source_extractions "
            "(extraction_id, source, indicator, qualifiers_json, status, network_called, "
            "started_at, finished_at, received_count, valid_count, persisted_count, request_count, run_id, task_key) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (f"{run_id}-{index}", "bcb_sgs", "226", qualifiers, status, 1,
             f"2026-10-27T00:00:0{index}+00:00", f"2026-10-27T00:00:0{index}+00:00",
             count, count, count, 1, row_run_id, task),
        )

    rows = store.source_extractions_for_refresh_run(run_id)
    assert {row["task_key"] for row in rows} == {
        "sgs_juros_w226_2025", "sgs_inflacao_w433_2025",
    }
    assert {row["qualifiers_json"] for row in rows} == {
        '{"identity_kind":"full_period"}', '{"unit":"pct_period"}',
    }
    assert {row["persisted_count"] for row in rows} == {3, 4}
    assert store._query(
        "SELECT status FROM source_extractions WHERE extraction_id = ?", (f"{run_id}-3",)
    )[0]["status"] == "success"


def test_pg17_refresh_status_endpoint_reads_v5_run_without_mutation(
    isolated_pg17_schema: None,
) -> None:
    from fastapi.testclient import TestClient

    from main import app

    run_id = f"pg-status-{uuid4().hex}"
    now = datetime.now(timezone.utc).isoformat()
    store._execute(
        "INSERT INTO refresh_runs (run_id, cycle_id, state, force, manifest_version, created_at, updated_at, finished_at) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
        (run_id, run_id, "partial", 0, "daily-refresh-v5", now, now, now),
    )
    store._execute(
        "INSERT INTO source_extractions "
        "(extraction_id, source, indicator, qualifiers_json, status, network_called, started_at, finished_at, "
        "received_count, valid_count, persisted_count, request_count, run_id, task_key) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (f"{run_id}-extraction", "bcb_sgs", "226", '{"unit":"pct_period"}', "partial", 1,
         now, now, 1, 1, 1, 1, run_id, "sgs_inflacao_w433_2025"),
    )
    for task, status, success, error in (
        ("pg_status_failed", "error", None, "provider_unavailable"),
        ("pg_status_succeeded", "ok", now, None),
    ):
        store._execute(
            "INSERT INTO refresh_task_status "
            "(task, provider, last_attempt, last_success, status, series_expected, series_updated, record_count, error) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (task, "fixture", now, success, status, 1, 0 if error else 1, 0 if error else 1, error),
        )
    before = store._query("SELECT state, final_result FROM refresh_runs WHERE run_id = ?", (run_id,))[0]
    with TestClient(app) as client:
        response = client.get("/api/refresh-status")
    after = store._query("SELECT state, final_result FROM refresh_runs WHERE run_id = ?", (run_id,))[0]
    assert response.status_code == 200
    assert response.json()["overall_status"] == "partial"
    tasks = {row["task"]: row for row in response.json()["tasks"]}
    assert tasks["pg_status_failed"]["status"] == "error"
    assert tasks["pg_status_succeeded"]["status"] == "ok"
    assert before == after


def test_pg17_db_stats_unions_legacy_and_canonical_focus_identities(
    isolated_pg17_schema: None,
) -> None:
    date = datetime.now(timezone.utc).date().isoformat()
    store._execute(
        "INSERT INTO focus_forecasts (indicator, date, identity_key, data_json) "
        "VALUES (?, ?, ?, ?)",
        ("IPCA", date, "2026|0|S", '{"Data":"' + date + '","DataReferencia":2026,"baseCalculo":0,"Suavizada":"S"}'),
    )
    store._execute(
        "INSERT INTO focus_forecasts (indicator, date, identity_key, data_json) "
        "VALUES (?, ?, ?, ?)",
        ("IPCA", date, "2027|0|S", '{"Data":"' + date + '","DataReferencia":2027,"baseCalculo":0,"Suavizada":"S"}'),
    )
    store._execute(
        "INSERT INTO focus (indicator, date, data_json) VALUES (?, ?, ?)",
        ("IPCA", date, '{"Data":"' + date + '","DataReferencia":2026,"baseCalculo":0,"Suavizada":"S"}'),
    )
    store._execute(
        "INSERT INTO focus (indicator, date, data_json) VALUES (?, ?, ?)",
        ("Selic", date, '{"Data":"' + date + '","DataReferencia":2026,"baseCalculo":1,"Suavizada":""}'),
    )

    assert store.db_stats()["focus"] == 3


def test_pg17_b3_cached_curve_has_single_cache_hit_extraction(
    isolated_pg17_schema: None,
) -> None:
    trade_day = datetime(2026, 10, 2, tzinfo=timezone.utc).date()
    token = set_refresh_context("pg-b3-cache", "b3_di", "pg-b3-cache")
    try:
        with (
            patch.object(b3_di, "list_business_days", return_value=[trade_day]),
            patch.object(b3_di, "get_b3_di_dates", return_value=[trade_day.isoformat()]),
            patch.object(b3_di, "query_observation_vintages", return_value=[
                {"reference_period": trade_day.isoformat()}
            ]),
            patch.object(b3_di, "query_b3_di", return_value=[{
                "trade_date": trade_day.isoformat(), "symbol": "DI1V27",
                "maturity": "2027-10-01", "rate": 11.5,
            }]),
            patch.object(b3_di, "_download_day") as download,
        ):
            curves = b3_di.fetch_di_curves(days=1)
    finally:
        reset_refresh_context(token)

    rows = store._query("SELECT * FROM source_extractions WHERE run_id = ?", ("pg-b3-cache",))
    download.assert_not_called()
    assert curves[trade_day.isoformat()][0]["rate"] == 11.5
    assert len(rows) == 1
    assert (rows[0]["status"], rows[0]["network_called"]) == ("cache_hit", 0)
    assert rows[0]["extracted_at"] is None


def test_pg17_b3_mixed_curve_refresh_logs_one_network_extraction(
    isolated_pg17_schema: None,
) -> None:
    cached_day = datetime(2026, 10, 1, tzinfo=timezone.utc).date()
    fetched_day = datetime(2026, 10, 2, tzinfo=timezone.utc).date()
    token = set_refresh_context("pg-b3-mixed", "b3_di", "pg-b3-mixed")
    try:
        with (
            patch.object(b3_di, "list_business_days", return_value=[cached_day, fetched_day]),
            patch.object(b3_di, "get_b3_di_dates", return_value=[cached_day.isoformat()]),
            patch.object(b3_di, "query_observation_vintages", return_value=[
                {"reference_period": cached_day.isoformat()}
            ]),
            patch.object(b3_di, "query_b3_di", return_value=[{
                "trade_date": cached_day.isoformat(), "symbol": "DI1V27",
                "maturity": "2027-10-01", "rate": 11.4,
            }]),
            patch.object(b3_di, "_download_day", return_value=[{
                "trade_date": fetched_day.isoformat(), "symbol": "DI1V27",
                "maturity": "2027-10-01", "rate": 11.5,
            }]) as download,
        ):
            b3_di.fetch_di_curves(days=2)
    finally:
        reset_refresh_context(token)

    rows = store._query("SELECT * FROM source_extractions WHERE run_id = ?", ("pg-b3-mixed",))
    download.assert_called_once_with(fetched_day)
    assert len(rows) == 1
    assert (rows[0]["status"], rows[0]["network_called"]) == ("success", 1)
    assert rows[0]["received_count"] == rows[0]["valid_count"] == 1
    assert rows[0]["source_reference_min"] == fetched_day.isoformat()


def test_pg17_sgs_annual_window_persists_actual_dates_then_records_window_cache_hit(
    isolated_pg17_schema: None,
) -> None:
    cached_period = {"data": "01/03/2015", "dataFim": "31/03/2015", "valor": "0.7"}
    store.upsert_tr_periods([cached_period])
    response = MagicMock()
    response.json.return_value = [
        {"data": "02/01/2015", "dataFim": "01/02/2015", "valor": "0"},
    ]
    token = set_refresh_context("pg-sgs-window-run", "sgs_juros_w226_2015", "pg-sgs-window-cycle")
    try:
        with patch.object(bcb_sgs, "get_provider_response_bounded", return_value=response) as network:
            fetched = bcb_sgs.fetch_sgs_window(
                226,
                start_date="01/01/2015",
                end_date="31/12/2015",
                use_cache=False,
                fail_fast=True,
                request_timeout=20,
            )
            cached = bcb_sgs.fetch_sgs_window(
                226,
                start_date="01/01/2015",
                end_date="31/12/2015",
                use_cache=True,
                fail_fast=True,
                request_timeout=20,
            )
    finally:
        reset_refresh_context(token)

    rows = store._query(
        "SELECT source, indicator, status, network_called, source_data_date, received_count, "
        "valid_count, persisted_count, extracted_at, run_id, task_key, cycle_id "
        "FROM source_extractions ORDER BY started_at"
    )
    expected_rows = [
        {"data": "02/01/2015", "dataFim": "01/02/2015", "valor": "0.0"},
    ]
    all_periods = [*expected_rows, cached_period]
    assert fetched == cached == all_periods
    network.assert_called_once()
    assert bcb_sgs.query_tr_periods("01/01/2015", "31/12/2015") == all_periods
    assert store._query("SELECT version FROM schema_migrations WHERE version = ?", ("014_tr_period_observations",))
    period_snapshot = store.source_audit_rows()["tr_periods"]
    assert len(period_snapshot) == 2
    assert {row["period_end"].isoformat() for row in period_snapshot} == {"2015-02-01", "2015-03-31"}
    assert [(row["status"], row["network_called"], row["received_count"], row["valid_count"]) for row in rows] == [
        ("success", 1, 1, 1), ("cache_hit", 0, 0, 0),
    ]
    assert rows[0]["source_data_date"] == "2015-01-02"
    assert rows[0]["received_count"] == rows[0]["valid_count"] == 1
    assert rows[0]["persisted_count"] == 1
    assert rows[0]["extracted_at"] is not None
    assert rows[0]["run_id"] == "pg-sgs-window-run"
    assert rows[0]["task_key"] == "sgs_juros_w226_2015"
    assert rows[0]["cycle_id"] == "pg-sgs-window-cycle"
    assert rows[1]["extracted_at"] is None


def test_pg17_tr_revision_capture_preserves_prior_asof_value(isolated_pg17_schema: None) -> None:
    original = {"data": "01/03/2015", "dataFim": "31/03/2015", "valor": "0.1"}
    revised = {**original, "valor": "0.2"}
    assert store.upsert_tr_periods([original]) == 1
    prior_capture = store._query(
        "SELECT first_seen_at FROM tr_period_observations WHERE period_start = ?",
        ("2015-03-01",),
    )[0]["first_seen_at"]
    assert store.upsert_tr_periods([revised]) == 1
    assert store.upsert_tr_periods([revised]) == 0
    assert store.query_tr_periods(known_at=prior_capture.isoformat()) == [original]
    assert store.query_tr_periods() == [revised]
    revisions = store._query(
        "SELECT value, first_seen_at, payload_hash FROM tr_period_observations WHERE period_start = ?",
        ("2015-03-01",),
    )
    assert len(revisions) == 2
    assert revisions[0]["value"] == 0.1
    assert revisions[1]["value"] == 0.2
    assert revisions[0]["payload_hash"] != revisions[1]["payload_hash"]


def test_pg17_half_open_lease_admits_only_one_concurrent_probe(
    isolated_pg17_schema: None,
) -> None:
    scope = "bcb_sgs:primary:primary"
    now = datetime.now(timezone.utc)
    now_text = now.isoformat()
    store._execute(
        """INSERT INTO source_access_state
           (scope_id, consecutive_temporary_failures, state, opened_until, updated_at)
           VALUES (?, 3, 'open', ?, ?)
           ON CONFLICT(scope_id) DO UPDATE SET consecutive_temporary_failures = 3,
             state = 'open', opened_until = excluded.opened_until,
             retry_not_before = NULL, probe_lease_until = NULL,
             updated_at = excluded.updated_at""",
        (scope, (now - timedelta(seconds=1)).isoformat(), now_text),
    )
    barrier = Barrier(8)

    def try_probe(index: int) -> bool:
        barrier.wait(timeout=10)
        try:
            admitted, _, half_open, _generation = store.claim_source_access_probe(
                scope, now_text, (now + timedelta(minutes=1)).isoformat()
            )
            return admitted and half_open
        finally:
            connection = getattr(store._pg_local, "conn", None)
            if connection is not None:
                connection.close()

    with ThreadPoolExecutor(max_workers=8) as executor:
        results = list(executor.map(try_probe, range(8)))
    assert sum(results) == 1
    state = store._query(
        "SELECT state, probe_lease_until FROM source_access_state WHERE scope_id = ?",
        (scope,),
    )[0]
    assert state["state"] == "half_open"
    assert state["probe_lease_until"] is not None


def test_pg17_stale_success_generation_preserves_new_failure_and_probe_lease(
    isolated_pg17_schema: None,
) -> None:
    scope = "bcb_sgs:primary:pg-causal"
    instant = datetime(2026, 10, 27, 12, tzinfo=timezone.utc)
    admitted, _, _, old_generation = store.claim_source_access_probe(
        scope, instant.isoformat(), (instant + timedelta(seconds=30)).isoformat(),
    )
    assert admitted
    # A later provider failure supersedes the successful worker's ticket.
    failure_at = instant + timedelta(seconds=1)
    store.record_source_access_failure(
        scope, failure_at.isoformat(), (failure_at + timedelta(minutes=5)).isoformat(),
        None, rate_limited=False, failure_count=3,
    )
    probe_at = failure_at + timedelta(minutes=6)
    reclaimed, _, half_open, current_generation = store.claim_source_access_probe(
        scope, probe_at.isoformat(), (probe_at + timedelta(minutes=1)).isoformat(),
    )
    assert reclaimed and half_open and current_generation > old_generation
    before = store._query("SELECT * FROM source_access_state WHERE scope_id = ?", (scope,))[0]

    assert not store.record_source_access_success(scope, instant.isoformat(), old_generation)
    after = store._query("SELECT * FROM source_access_state WHERE scope_id = ?", (scope,))[0]
    assert after == before

    assert store.record_source_access_success(scope, probe_at.isoformat(), current_generation)
    recovered = store._query("SELECT * FROM source_access_state WHERE scope_id = ?", (scope,))[0]
    assert recovered["state"] == "closed"
    assert recovered["probe_lease_until"] is None
    assert recovered["updated_at"] >= before["updated_at"]


def test_pg17_sgs_validated_response_then_actual_transaction_failure_is_not_network_outage(
    isolated_pg17_schema: None, monkeypatch: pytest.MonkeyPatch,
) -> None:
    from db.store import source_access_states

    store._execute("""CREATE FUNCTION reject_sgs_write() RETURNS trigger LANGUAGE plpgsql AS $$
        BEGIN RAISE EXCEPTION 'test persistence rejection'; END $$""")
    store._execute("CREATE TRIGGER reject_sgs_write BEFORE INSERT ON sgs "
                   "FOR EACH ROW EXECUTE FUNCTION reject_sgs_write()")
    payload = [{"data": "01/01/2026", "valor": "1.25"}]
    response = MagicMock()
    response.json.return_value = payload
    response.content = b'[{"data":"01/01/2026","valor":"1.25"}]'
    def admitted_response(*_args, **_kwargs):
        budget = current_retry_budget()
        assert budget is not None
        budget.reserve_request()
        return response

    monkeypatch.setattr(bcb_sgs, "_window_cache_fresh", lambda *_args, **_kwargs: False)
    token = set_refresh_context("pg-sgs-write-failure", "sgs_1635", "pg-sgs-cycle")
    try:
        with (
            patch.object(bcb_sgs, "get_provider_response_bounded", side_effect=admitted_response),
            pytest.raises(RaiseException),
        ):
            bcb_sgs.fetch_sgs_window(
                1635, start_date="01/01/2026", end_date="31/12/2026",
                use_cache=False, fail_fast=True, request_timeout=20,
            )
    finally:
        reset_refresh_context(token)

    extraction = store._query(
        "SELECT status, error_category, network_called FROM source_extractions "
        "WHERE run_id = ?", ("pg-sgs-write-failure",),
    )[0]
    state = source_access_states()["bcb_sgs:primary:primary"]
    assert extraction["status"] == "persistence_error"
    assert extraction["error_category"] == "persistence_failure"
    assert extraction["network_called"] == 1
    assert state["state"] == "closed"
    assert store._query("SELECT * FROM sgs WHERE series_code = ?", (1635,)) == []


def test_pg17_sgs_expired_total_deadline_keeps_old_cohort_and_no_new_rows(
    isolated_pg17_schema: None, monkeypatch: pytest.MonkeyPatch,
) -> None:
    previous = [{"data": "01/01/2026", "valor": "1.0"}]
    store.upsert_sgs(1635, previous)
    original_upsert = bcb_sgs.upsert_sgs

    def expired_transaction(code, records, **kwargs):
        return original_upsert(code, records, deadline_at=monotonic() - 1)

    monkeypatch.setattr(bcb_sgs, "upsert_sgs", expired_transaction)
    response = MagicMock()
    response.json.return_value = [{"data": "02/01/2026", "valor": "2.0"}]
    response.content = b'[{"data":"02/01/2026","valor":"2.0"}]'
    monkeypatch.setattr(bcb_sgs, "_window_cache_fresh", lambda *_args, **_kwargs: False)
    token = set_refresh_context("pg-sgs-expired-total", "sgs_1635", "pg-sgs-cycle")
    try:
        with (
            patch.object(bcb_sgs, "get_provider_response_bounded", return_value=response),
            pytest.raises(TimeoutError, match="deadline"),
        ):
            bcb_sgs.fetch_sgs_window(
                1635, start_date="01/01/2026", end_date="31/12/2026",
                use_cache=False, fail_fast=True, request_timeout=20,
            )
    finally:
        reset_refresh_context(token)

    rows = store._query("SELECT date, value FROM sgs WHERE series_code = ?", (1635,))
    extraction = store._query(
        "SELECT status, error_category FROM source_extractions WHERE run_id = ?",
        ("pg-sgs-expired-total",),
    )[0]
    assert rows == [{"date": "2026-01-01", "value": 1.0}]
    assert extraction["status"] == "persistence_error"
    assert extraction["error_category"] == "persistence_failure"


def test_pg17_sgs_sql_after_network_window_budget_but_before_total_deadline_succeeds(
    isolated_pg17_schema: None, monkeypatch: pytest.MonkeyPatch,
) -> None:
    clock_value = [100.0]
    budget = RetryBudget(
        retry_profile("bcb_sgs", {"window_policy": "calendar-year-v1", "window_count": 1}),
        clock=lambda: clock_value[0], scope_id="bcb_sgs:primary:primary",
    )
    budget_token = install_retry_budget(budget)
    monkeypatch.setattr(store, "monotonic", lambda: clock_value[0])
    response = MagicMock()
    response.json.return_value = [{"data": "01/01/2026", "valor": "1.25"}]
    response.content = b'[{"data":"01/01/2026","valor":"1.25"}]'
    monkeypatch.setattr(bcb_sgs, "_window_cache_fresh", lambda *_args, **_kwargs: False)
    original_upsert = bcb_sgs.upsert_sgs

    def delayed_database_write(code, records, **kwargs):
        clock_value[0] += 21  # network window is 20 s; total budget is 90 s.
        return original_upsert(code, records, **kwargs)

    monkeypatch.setattr(bcb_sgs, "upsert_sgs", delayed_database_write)
    token = set_refresh_context("pg-sgs-sql-headroom", "sgs_1635", "pg-sgs-cycle")
    try:
        with patch.object(bcb_sgs, "get_provider_response_bounded", return_value=response):
            bcb_sgs.fetch_sgs_window(
                1635, start_date="01/01/2026", end_date="31/12/2026",
                use_cache=False, fail_fast=True, request_timeout=20,
            )
    finally:
        reset_refresh_context(token)
        reset_retry_budget(budget_token)

    extraction = store._query(
        "SELECT status, error_category, extracted_at FROM source_extractions WHERE run_id = ?",
        ("pg-sgs-sql-headroom",),
    )[0]
    assert extraction["status"] == "success"
    assert extraction["error_category"] is None
    assert extraction["extracted_at"] is not None
    assert store._query("SELECT * FROM sgs WHERE series_code = ?", (1635,))


def test_pg17_sgs_total_deadline_after_sql_writes_but_before_commit_rolls_back(
    isolated_pg17_schema: None, monkeypatch: pytest.MonkeyPatch,
) -> None:
    store.upsert_sgs(1635, [{"data": "01/01/2026", "valor": "1.0"}])
    response = MagicMock()
    response.json.return_value = [{"data": "02/01/2026", "valor": "2.0"}]
    response.content = b'[{"data":"02/01/2026","valor":"2.0"}]'
    monkeypatch.setattr(bcb_sgs, "_window_cache_fresh", lambda *_args, **_kwargs: False)
    original_upsert = bcb_sgs.upsert_sgs

    def expire_between_write_and_commit(code, records, **kwargs):
        deadline_at = kwargs["deadline_at"]
        ticks = iter((deadline_at - 1, deadline_at + 1))
        monkeypatch.setattr(store, "monotonic", lambda: next(ticks))
        return original_upsert(code, records, **kwargs)

    monkeypatch.setattr(bcb_sgs, "upsert_sgs", expire_between_write_and_commit)
    token = set_refresh_context("pg-sgs-midwrite-expiry", "sgs_1635", "pg-sgs-cycle")
    try:
        with (
            patch.object(bcb_sgs, "get_provider_response_bounded", return_value=response),
            pytest.raises(TimeoutError, match="before commit"),
        ):
            bcb_sgs.fetch_sgs_window(
                1635, start_date="01/01/2026", end_date="31/12/2026",
                use_cache=False, fail_fast=True, request_timeout=20,
            )
    finally:
        reset_refresh_context(token)

    assert store._query("SELECT date, value FROM sgs WHERE series_code = ?", (1635,)) == [
        {"date": "2026-01-01", "value": 1.0},
    ]
    assert store._query(
        "SELECT reference_period FROM observation_vintages "
        "WHERE provider = 'bcb_sgs' AND reference_period = ?", ("2026-01-02",),
    ) == []
    extraction = store._query(
        "SELECT status, error_category FROM source_extractions WHERE run_id = ?",
        ("pg-sgs-midwrite-expiry",),
    )[0]
    assert extraction["status"] == "persistence_error"
    assert extraction["error_category"] == "persistence_failure"


def test_pg17_future_publisher_retry_after_survives_late_probe_receipt_ack(
    isolated_pg17_schema: None,
) -> None:
    scope = "tesouro:primary:primary"
    started = datetime.now(timezone.utc)
    future = started + timedelta(hours=1)
    admitted, _, _, worker_generation = store.claim_source_access_probe(
        scope, started.isoformat(), (started + timedelta(minutes=2)).isoformat(),
    )
    assert admitted
    quota_committed = Event()
    state_before_ack: list[dict[str, object]] = []

    def publish_quota() -> None:
        try:
            store.record_source_access_failure(
                scope, (started + timedelta(seconds=1)).isoformat(), future.isoformat(),
                future.isoformat(), rate_limited=True,
            )
            state_before_ack.extend(
                store._query("SELECT * FROM source_access_state WHERE scope_id = ?", (scope,))
            )
            quota_committed.set()
        finally:
            connection = getattr(store._pg_local, "conn", None)
            if connection is not None:
                connection.close()

    def acknowledge_older_probe() -> None:
        assert quota_committed.wait(timeout=10)
        try:
            assert not store.record_source_access_success(
                scope, started.isoformat(), worker_generation
            )
        finally:
            connection = getattr(store._pg_local, "conn", None)
            if connection is not None:
                connection.close()

    with ThreadPoolExecutor(max_workers=2) as executor:
        futures = [executor.submit(publish_quota), executor.submit(acknowledge_older_probe)]
        for future_result in futures:
            future_result.result(timeout=15)
    state = store.get_source_access_state(scope)
    full_state = store._query("SELECT * FROM source_access_state WHERE scope_id = ?", (scope,))[0]
    assert state_before_ack and full_state == state_before_ack[0]
    assert state is not None
    assert state["state"] == "open"
    assert state["retry_not_before"] == future


def test_pg17_tesouro_actual_producer_commits_validated_csv_and_capture_receipt(
    isolated_pg17_schema: None,
) -> None:
    today = datetime.now(timezone.utc).date()
    maturity = today.replace(year=today.year + 5).strftime("%d/%m/%Y")
    base = today.strftime("%d/%m/%Y")
    body = (
        "Tipo Titulo;Data Vencimento;Data Base;Taxa Compra Manha;Taxa Venda Manha;"
        "PU Compra Manha;PU Venda Manha;PU Base Manha\n"
        f"Tesouro Prefixado;{maturity};{base};11,1;11,2;900,1;901,2;899,0\n"
    ).encode()
    response = MagicMock()
    response.content = body
    response.headers = {"content-length": str(len(body))}
    token = set_refresh_context("pg-tesouro-producer", "tesouro_quotes", "pg-tesouro-cycle")
    try:
        with patch.object(tesouro_direto, "get_provider_response_bounded", return_value=response):
            result = tesouro_direto.refresh_treasury_data()
    finally:
        reset_refresh_context(token)

    extraction = store._query(
        "SELECT status, network_called, extracted_at, received_count, persisted_count "
        "FROM source_extractions WHERE run_id = ?", ("pg-tesouro-producer",),
    )[0]
    commits = store._query("SELECT * FROM treasury_cohort_commits")
    rows = store._query("SELECT * FROM treasury")
    assert result["rows"] == 1
    assert extraction["status"] == "success"
    assert extraction["network_called"] == 1
    assert extraction["extracted_at"] is not None
    assert extraction["received_count"] == extraction["persisted_count"] == 1
    assert len(commits) == len(rows) == 1
    assert commits[0]["source_payload_sha256"]


def test_pg17_tesouro_v6_intent_replays_original_capture_without_new_http(
    isolated_pg17_schema: None,
) -> None:
    from db.refresh_run_models import (
        CycleId,
        RefreshRunStart,
        RefreshWorkItemSpec,
        RunId,
        WorkItemKey,
    )
    from db.refresh_runs import create_or_resume_run

    run_id = RunId("pg-tesouro-v6-replay")
    now = datetime.now(timezone.utc)
    create_or_resume_run(RefreshRunStart(
        run_id=run_id, cycle_id=CycleId(str(run_id)), force=True,
        manifest_version="daily-refresh-v6", derived_cutoff=None, macro_cutoff=None,
        items=(RefreshWorkItemSpec(WorkItemKey("tesouro"), "tesouro", 0),),
        now=now.isoformat(), lease_expires_at=(now + timedelta(minutes=30)).isoformat(),
    ))
    base = now.date().strftime("%d/%m/%Y")
    maturity = now.date().replace(year=now.year + 5).strftime("%d/%m/%Y")
    body = (
        "Tipo Titulo;Data Vencimento;Data Base;Taxa Compra Manha;Taxa Venda Manha;"
        "PU Compra Manha;PU Venda Manha;PU Base Manha\n"
        f"Tesouro Prefixado;{maturity};{base};11,1;11,2;900,1;901,2;899,0\n"
    ).encode()
    response = MagicMock()
    response.content = body
    response.headers = {"content-length": str(len(body))}
    token = set_refresh_context(str(run_id), "tesouro", str(run_id))
    try:
        with patch.object(tesouro_direto, "get_provider_response_bounded", return_value=response) as fetch:
            first = tesouro_direto.refresh_treasury_data()
            original_capture = store.get_meta("td_last_refresh")
            second = tesouro_direto.refresh_treasury_data()
        assert fetch.call_count == 1
    finally:
        reset_refresh_context(token)
    extraction = store._query(
        "SELECT network_called, received_count, persisted_count, request_count "
        "FROM source_extractions WHERE run_id = ? ORDER BY started_at DESC LIMIT 1",
        (str(run_id),),
    )[0]
    assert first == {"rows": 1}
    assert second["receipt_replay"] is True
    assert store.get_meta("td_last_refresh") == original_capture
    assert (
        extraction["network_called"], extraction["received_count"],
        extraction["persisted_count"], extraction["request_count"],
    ) == (0, 0, 0, 0)


def test_pg17_resumable_client_api_reaches_real_treasury_producer_over_local_http(
    isolated_pg17_schema: None, monkeypatch: pytest.MonkeyPatch,
) -> None:
    from fastapi.testclient import TestClient

    import provider_http
    from db.refresh_run_models import (
        CycleId,
        RefreshRunStart,
        RefreshWorkItemSpec,
        RunId,
        WorkItemKey,
    )
    from db.refresh_runs import create_or_resume_run
    from main import app
    from refresh_run_client import RefreshRunClientConfig, run_resumable_refresh

    class CsvHandler(BaseHTTPRequestHandler):
        calls = 0

        def do_GET(self) -> None:
            type(self).calls += 1
            today = datetime.now(timezone.utc).date()
            maturity = today.replace(year=today.year + 5).strftime("%d/%m/%Y")
            base = today.strftime("%d/%m/%Y")
            body = (
                "Tipo Titulo;Data Vencimento;Data Base;Taxa Compra Manha;Taxa Venda Manha;"
                "PU Compra Manha;PU Venda Manha;PU Base Manha\n"
                f"Tesouro Prefixado;{maturity};{base};11,1;11,2;900,1;901,2;899,0\n"
            ).encode()
            self.send_response(200)
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, _format: str, *_args: object) -> None:
            return

    server = ThreadingHTTPServer(("127.0.0.1", 0), CsvHandler)
    server.daemon_threads = True
    thread = Thread(target=server.serve_forever, daemon=True)
    thread.start()
    local_url = f"http://127.0.0.1:{server.server_port}/tesouro.csv"
    monkeypatch.setattr(provider_http, "require_allowed_provider_url", lambda _url: local_url)
    run_id = RunId("pg-tesouro-client-api")
    now = datetime.now(timezone.utc)
    create_or_resume_run(RefreshRunStart(
        run_id=run_id, cycle_id=CycleId(str(run_id)), force=False,
        manifest_version="daily-refresh-v6", derived_cutoff=None, macro_cutoff=None,
        items=(RefreshWorkItemSpec(WorkItemKey("tesouro"), "tesouro", 0),),
        now=now.isoformat(), lease_expires_at=(now + timedelta(minutes=30)).isoformat(),
    ))
    config = RefreshRunClientConfig(
        "http://testserver", str(run_id), str(run_id), "test-token", resume_existing=True,
        max_wait_seconds=30,
    )
    try:
        with TestClient(app) as client:
            def request_json(url, method, _timeout, headers=None, body=None):
                path = url.removeprefix("http://testserver")
                started = monotonic()
                response = client.request(method, path, headers=headers, json=body)
                return response.status_code, response.json(), monotonic() - started

            result = run_resumable_refresh(config, request_json)
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=1)

    assert result.status == "ok"
    assert CsvHandler.calls == 1
    assert tesouro_direto.TESOURO_DATASET_UUID == "796d2059-14e9-44e3-80c9-2d9e30b405c1"
    receipt = store._query(
        "SELECT run_id, task_key, contract_version, dataset_uuid, cohort_scope_sha256, "
        "cohort_rows_sha256 FROM treasury_cohort_commits"
    )[0]
    assert receipt["run_id"] == str(run_id)
    assert receipt["task_key"] == "tesouro"
    assert receipt["dataset_uuid"] == tesouro_direto.TESOURO_DATASET_UUID
    assert receipt["cohort_rows_sha256"]
    extraction = store._query(
        "SELECT network_called, request_count, request_trace_json FROM source_extractions "
        "WHERE run_id = ?", (str(run_id),),
    )[0]
    assert extraction["network_called"] == 1
    assert extraction["request_count"] == 1
    trace = json.loads(extraction["request_trace_json"])
    assert trace[0]["route_id"] == "tesouro_daily_csv"
    assert trace[0]["bytes_received"] is not None


def test_pg17_resumable_client_api_reaches_real_v6_sgs_producer_over_local_http(
    isolated_pg17_schema: None, monkeypatch: pytest.MonkeyPatch,
) -> None:
    from fastapi.testclient import TestClient

    import provider_http
    from db.refresh_run_models import (
        CycleId,
        RefreshRunStart,
        RefreshWorkItemSpec,
        RunId,
        WorkItemKey,
    )
    from db.refresh_runs import create_or_resume_run
    from db.sgs_refresh_plan import build_sgs_plan, freeze_sgs_plan, plan_item_task_name
    from main import app
    from refresh_run_client import RefreshRunClientConfig, run_resumable_refresh

    class SgsHandler(BaseHTTPRequestHandler):
        calls = 0
        observed_date = ""

        def do_GET(self) -> None:
            type(self).calls += 1
            payload = json.dumps([{"data": type(self).observed_date, "valor": "1,25"}]).encode()
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(payload)))
            self.end_headers()
            self.wfile.write(payload)

        def log_message(self, _format: str, *_args: object) -> None:
            return

    plan = build_sgs_plan(datetime.now(timezone.utc).date(), max_archive_units=0)
    unit = plan.units[0]
    SgsHandler.observed_date = unit.start_date.strftime("%d/%m/%Y")
    server = ThreadingHTTPServer(("127.0.0.1", 0), SgsHandler)
    server.daemon_threads = True
    thread = Thread(target=server.serve_forever, daemon=True)
    thread.start()
    local_url = f"http://127.0.0.1:{server.server_port}/sgs.json"
    monkeypatch.setattr(provider_http, "require_allowed_provider_url", lambda _url: local_url)
    run_id = RunId("pg-sgs-v6-client-api")
    now = datetime.now(timezone.utc)
    create_or_resume_run(RefreshRunStart(
        run_id=run_id, cycle_id=CycleId(str(run_id)), force=True,
        manifest_version="daily-refresh-v6", derived_cutoff=None, macro_cutoff=None,
        items=(RefreshWorkItemSpec(
            WorkItemKey(plan_item_task_name(unit)), plan_item_task_name(unit), 0,
        ),),
        now=now.isoformat(), lease_expires_at=(now + timedelta(minutes=30)).isoformat(),
    ))
    freeze_sgs_plan(str(run_id), plan, now)
    config = RefreshRunClientConfig(
        "http://testserver", str(run_id), str(run_id), "test-token", resume_existing=True,
        force=True, max_wait_seconds=30,
    )
    try:
        with TestClient(app) as client:
            def request_json(url, method, _timeout, headers=None, body=None):
                path = url.removeprefix("http://testserver")
                started = monotonic()
                response = client.request(method, path, headers=headers, json=body)
                return response.status_code, response.json(), monotonic() - started

            result = run_resumable_refresh(config, request_json)
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=1)

    assert result.status == "ok"
    assert SgsHandler.calls == 1
    extraction = store._query(
        "SELECT status, network_called, request_count, request_trace_json "
        "FROM source_extractions WHERE run_id = ?", (str(run_id),),
    )[0]
    assert extraction["status"] == "success"
    assert extraction["network_called"] == 1
    assert extraction["request_count"] == 1
    trace = json.loads(extraction["request_trace_json"])
    assert trace[0]["bytes_received"] is not None
    assert store._query("SELECT * FROM sgs WHERE series_code = ?", (unit.series_code,))


def test_pg17_annual_1207_client_api_persists_raw_brl_and_v2_unit_proof(
    isolated_pg17_schema: None, monkeypatch: pytest.MonkeyPatch,
) -> None:
    from fastapi.testclient import TestClient

    import provider_http
    from db.refresh_run_models import (
        CycleId,
        RefreshRunStart,
        RefreshWorkItemSpec,
        RunId,
        WorkItemKey,
    )
    from db.refresh_runs import create_or_resume_run
    from db.sgs_refresh_plan import build_sgs_plan, freeze_sgs_plan, plan_item_task_name
    from main import app
    from refresh_run_client import RefreshRunClientConfig, run_resumable_refresh

    class SgsHandler(BaseHTTPRequestHandler):
        requested_path = ""

        def do_GET(self) -> None:
            type(self).requested_path = self.path
            payload = (
                b'[{"data":"01/01/2024","valor":"11779250581628.30"},'
                b'{"data":"01/01/2025","valor":"12738565613881.10"}]'
            )
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(payload)))
            self.end_headers()
            self.wfile.write(payload)

        def log_message(self, _format: str, *_args: object) -> None:
            return

    plan = build_sgs_plan(date(2026, 10, 28), max_archive_units=0)
    unit = next(item for item in plan.units if item.series_code == 1207)
    server = ThreadingHTTPServer(("127.0.0.1", 0), SgsHandler)
    server.daemon_threads = True
    thread = Thread(target=server.serve_forever, daemon=True)
    thread.start()
    monkeypatch.setattr(
        provider_http, "require_allowed_provider_url",
        lambda _url: f"http://127.0.0.1:{server.server_port}/sgs.json",
    )
    run_id = RunId(f"pg-sgs-1207-{uuid4().hex[:10]}")
    now = datetime.now(timezone.utc)
    task_name = plan_item_task_name(unit)
    create_or_resume_run(RefreshRunStart(
        run_id=run_id, cycle_id=CycleId(str(run_id)), force=True,
        manifest_version="daily-refresh-v6", derived_cutoff=None, macro_cutoff=None,
        items=(RefreshWorkItemSpec(WorkItemKey(task_name), task_name, 0),),
        now=now.isoformat(), lease_expires_at=(now + timedelta(minutes=30)).isoformat(),
    ))
    freeze_sgs_plan(str(run_id), plan, now)
    config = RefreshRunClientConfig(
        "http://testserver", str(run_id), str(run_id), "test-token", resume_existing=True,
        force=True, max_wait_seconds=30,
    )

    try:
        with TestClient(app) as client:
            def request_json(url, method, _timeout, headers=None, body=None):
                started = monotonic()
                response = client.request(
                    method, url.removeprefix("http://testserver"), headers=headers, json=body,
                )
                return response.status_code, response.json(), monotonic() - started

            result = run_resumable_refresh(config, request_json)
            activity = client.get("/api/atividade/pib")
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=1)

    assert result.status == "ok"
    assert "dataInicial=01%2F01%2F2020" in SgsHandler.requested_path
    assert "dataFinal=28%2F10%2F2026" in SgsHandler.requested_path
    assert activity.status_code == 200
    values = activity.json()["data"]
    assert [row["valor"] for row in values] == [
        "11779250581628.3", "12738565613881.1",
    ]
    frozen = store._query(
        "SELECT contract_version, unit, frequency, start_date, end_date FROM sgs_refresh_plan_units "
        "WHERE run_id = ? AND unit_id = ?", (str(run_id), unit.unit_id),
    )[0]
    assert frozen == {
        "contract_version": "acquisition-contract-v2", "unit": "brl", "frequency": "annual",
        "start_date": "2020-01-01", "end_date": "2026-10-28",
    }
    receipt = store._query(
        "SELECT contract_version, requested_start, requested_end, observed_count FROM sgs_unit_commits "
        "WHERE run_id = ? AND series_code = 1207", (str(run_id),),
    )[0]
    assert receipt == {
        "contract_version": "acquisition-contract-v2", "requested_start": "2020-01-01",
        "requested_end": "2026-10-28", "observed_count": 2,
    }


def test_pg17_corrupt_1207_archive_intent_is_preserved_but_not_carried_forward(
    isolated_pg17_schema: None,
) -> None:
    from db.sgs_refresh_plan import (
        build_next_sgs_plan,
        build_sgs_plan,
        freeze_sgs_plan,
        set_archive_unit_state,
    )

    old = build_sgs_plan(
        date(2026, 10, 27), max_archive_units=1, last_archive_series_code=1206,
    )
    archive = next(unit for unit in old.units if unit.series_code == 1207 and unit.role != "recent")
    freeze_sgs_plan("pg-corrupt-pending-old", old)
    assert set_archive_unit_state(
        "pg-corrupt-pending-old", archive.unit_id, "deferred", "LegacyPending",
    )
    store._execute(
        "UPDATE sgs_refresh_plan_units SET unit = 'brl_million', frequency = 'quarterly' "
        "WHERE run_id = ? AND unit_id = ?",
        ("pg-corrupt-pending-old", archive.unit_id),
    )
    old_hash = store._query(
        "SELECT plan_sha256 FROM sgs_refresh_plans WHERE run_id = ?",
        ("pg-corrupt-pending-old",),
    )[0]["plan_sha256"]

    next_plan = build_next_sgs_plan(date(2026, 10, 28), max_archive_units=1)
    freeze_sgs_plan("pg-corrupt-pending-new", next_plan)

    persisted_old = store._query(
        "SELECT unit, frequency, contract_version, status FROM sgs_refresh_plan_units "
        "WHERE run_id = ? AND unit_id = ?",
        ("pg-corrupt-pending-old", archive.unit_id),
    )[0]
    assert persisted_old == {
        "unit": "brl_million", "frequency": "quarterly",
        "contract_version": "acquisition-contract-v2", "status": "deferred",
    }
    assert store._query(
        "SELECT plan_sha256 FROM sgs_refresh_plans WHERE run_id = ?",
        ("pg-corrupt-pending-old",),
    )[0]["plan_sha256"] == old_hash
    assert all(
        (unit.contract_version, unit.unit, unit.frequency, unit.contract_id,
         unit.identity_kind, unit.method)
        == ("acquisition-contract-v2", "brl", "annual", "sgs:1207", "point", "date_range")
        for unit in next_plan.units if unit.series_code == 1207
    )


def test_pg17_sgs_cache_hit_cannot_close_an_open_primary_circuit(
    isolated_pg17_schema: None, monkeypatch: pytest.MonkeyPatch,
) -> None:
    scope = "bcb_sgs:primary:primary"
    now = datetime.now(timezone.utc)
    store.upsert_sgs(1635, [{"data": "01/01/2026", "valor": "1.25"}])
    store._execute(
        """INSERT INTO source_access_state
           (scope_id, consecutive_temporary_failures, state, opened_until, updated_at)
           VALUES (?, 3, 'open', ?, ?)
           ON CONFLICT(scope_id) DO UPDATE SET state = 'open', opened_until = excluded.opened_until,
             retry_not_before = NULL, probe_lease_until = NULL""",
        (scope, (now - timedelta(seconds=1)).isoformat(), now.isoformat()),
    )
    monkeypatch.setattr(bcb_sgs, "_window_cache_fresh", lambda *_args, **_kwargs: True)
    result = bcb_sgs.fetch_sgs_window(
        1635, start_date="01/01/2026", end_date="31/12/2026", use_cache=True,
    )
    state = store.get_source_access_state(scope)
    assert result == [{"data": "01/01/2026", "valor": "1.25"}]
    assert state is not None and state["state"] == "open"


def test_pg17_sgs_invalid_window_identity_cannot_close_half_open_circuit(
    isolated_pg17_schema: None, monkeypatch: pytest.MonkeyPatch,
) -> None:
    scope = "bcb_sgs:primary:primary"
    now = datetime.now(timezone.utc)
    store._execute(
        """INSERT INTO source_access_state
           (scope_id, consecutive_temporary_failures, state, opened_until, updated_at)
           VALUES (?, 3, 'open', ?, ?)
           ON CONFLICT(scope_id) DO UPDATE SET state = 'open', opened_until = excluded.opened_until,
             retry_not_before = NULL, probe_lease_until = NULL""",
        (scope, (now - timedelta(seconds=1)).isoformat(), now.isoformat()),
    )
    monkeypatch.setattr(bcb_sgs, "_window_cache_fresh", lambda *_args, **_kwargs: False)
    response = MagicMock()
    response.json.return_value = [{"data": "01/01/2024", "valor": "1.0"}]
    response.content = b'[{"data":"01/01/2024","valor":"1.0"}]'

    def one_http_then_wrong_identity(*_args, **_kwargs):
        from ingestion_policy import current_retry_budget

        budget = current_retry_budget()
        assert budget is not None
        budget.reserve_request()
        return response

    token = set_refresh_context("pg-sgs-invalid-identity", "sgs_1635", "pg-sgs-cycle")
    try:
        with (
            patch.object(bcb_sgs, "get_provider_response_bounded", side_effect=one_http_then_wrong_identity),
            pytest.raises(bcb_sgs.InvalidSGSPayloadError),
        ):
            bcb_sgs.fetch_sgs_window(
                1635, start_date="01/01/2026", end_date="31/12/2026",
                use_cache=False, fail_fast=True, request_timeout=20,
            )
    finally:
        reset_refresh_context(token)

    state = store.get_source_access_state(scope)
    assert state is not None and state["state"] == "half_open"
    assert store._query("SELECT * FROM sgs WHERE series_code = ?", (1635,)) == []


def test_pg17_advance_route_race_defers_real_sgs_producer_without_http(
    isolated_pg17_schema: None, monkeypatch: pytest.MonkeyPatch,
) -> None:
    from starlette.testclient import TestClient

    import provider_http
    import refresh_run_execution
    from datafetchers import bcb_sgs
    from main import app
    from refresh import RefreshDependencies

    dependencies = RefreshDependencies(fetch_sgs_window=bcb_sgs.fetch_sgs_window)
    monkeypatch.setattr(refresh_run_execution, "_dependencies", lambda _value: dependencies)
    monkeypatch.setattr(bcb_sgs, "get_provider_response_bounded", provider_http.get_provider_response_bounded)
    network_calls = 0

    def no_live_http(*_args, **_kwargs):
        nonlocal network_calls
        network_calls += 1
        raise AssertionError("network must be refused at provider admission")

    monkeypatch.setattr(provider_http.httpx.AsyncClient, "stream", no_live_http)
    execute = refresh_run_execution.execute_refresh_item

    def close_after_schedule_peek(task, snapshot, active, now):
        store._execute(
            """INSERT INTO source_access_state
               (scope_id, consecutive_temporary_failures, state, retry_not_before, updated_at)
               VALUES ('bcb_sgs:primary:primary', 3, 'open', ?, ?)
               ON CONFLICT(scope_id) DO UPDATE SET state='open', retry_not_before=excluded.retry_not_before,
                 opened_until=NULL, probe_lease_until=NULL""",
            ((now + timedelta(hours=1)).isoformat(), now.isoformat()),
        )
        return execute(task, snapshot, active, now)

    monkeypatch.setattr(refresh_run_execution, "execute_refresh_item", close_after_schedule_peek)
    with TestClient(app) as client:
        started = client.post(
            "/api/refresh-runs",
            json={"run_id": "pg-race-run", "cycle_id": "pg-race-run", "force": True},
        )
        item = started.json()["items"][0]
        advanced = client.post(
            "/api/refresh-runs/pg-race-run/advance",
            json={"expected_item_key": item["item_key"]},
        )

    assert network_calls == 0
    assert started.status_code == advanced.status_code == 200
    row = store._query(
        "SELECT status, attempt, claim_generation, acquisition_attempts, next_attempt_at, "
        "deferral_reason, claim_token FROM refresh_work_items WHERE run_id = ? AND item_key = ?",
        ("pg-race-run", item["item_key"]),
    )[0]
    assert row["status"] == "pending"
    assert row["attempt"] == row["claim_generation"] == 1
    assert row["acquisition_attempts"] == 0
    assert row["next_attempt_at"] is not None
    assert row["deferral_reason"] == "publisher_cooldown"
    assert row["claim_token"] is None


def test_pg17_outbound_marker_is_idempotent_and_stale_deferral_is_fenced(
    isolated_pg17_schema: None,
) -> None:
    from db.refresh_attempt import begin_refresh_attempt, new_refresh_attempt
    from db.refresh_run_models import (
        ClaimToken,
        CycleId,
        RefreshRunStart,
        RefreshWorkItemClaimRequest,
        RefreshWorkItemDeferral,
        RefreshWorkItemSpec,
        RunId,
        WorkItemKey,
    )
    from db.refresh_runs import (
        claim_next_work_item,
        create_or_resume_run,
        defer_work_item_claim,
    )

    now = datetime.now(timezone.utc)
    run_id = RunId("pg-marker-fence")
    item_key = WorkItemKey("focus")
    create_or_resume_run(RefreshRunStart(
        run_id=run_id,
        cycle_id=CycleId("pg-marker-fence"),
        force=True,
        manifest_version="daily-refresh-v6",
        derived_cutoff=None,
        macro_cutoff=None,
        items=(RefreshWorkItemSpec(item_key, "focus", 0),),
        now=now.isoformat(),
        lease_expires_at=(now + timedelta(minutes=30)).isoformat(),
    ))
    first = claim_next_work_item(RefreshWorkItemClaimRequest(
        run_id, now.isoformat(), (now + timedelta(minutes=10)).isoformat(), item_key,
    ))
    assert first is not None
    attempt = new_refresh_attempt(first)
    begin_refresh_attempt(attempt, now.isoformat())
    store.mark_refresh_acquisition_started(attempt, now.isoformat())
    store.mark_refresh_acquisition_started(attempt, now.isoformat())
    row = store._query(
        "SELECT acquisition_attempts FROM refresh_work_items WHERE run_id = ? AND item_key = ?",
        (str(run_id), str(item_key)),
    )[0]
    assert row["acquisition_attempts"] == 1

    expired = now + timedelta(seconds=30)
    store._execute(
        "UPDATE refresh_work_items SET claim_expires_at = ? WHERE run_id = ? AND item_key = ?",
        (expired.isoformat(), str(run_id), str(item_key)),
    )
    replacement_now = now + timedelta(seconds=31)
    replacement = claim_next_work_item(RefreshWorkItemClaimRequest(
        run_id, replacement_now.isoformat(), (replacement_now + timedelta(minutes=10)).isoformat(), item_key,
    ))
    assert replacement is not None and replacement.claim_token != first.claim_token
    with pytest.raises(RuntimeError, match="no longer current"):
        store.mark_refresh_acquisition_started(attempt, replacement_now.isoformat())
    assert not defer_work_item_claim(RefreshWorkItemDeferral(
        run_id, item_key, ClaimToken(str(first.claim_token)),
        (replacement_now + timedelta(minutes=1)).isoformat(), "retry_after", replacement_now.isoformat(),
    ))
    current = store._query(
        "SELECT status, claim_token, claim_generation, acquisition_attempts "
        "FROM refresh_work_items WHERE run_id = ? AND item_key = ?",
        (str(run_id), str(item_key)),
    )[0]
    assert current["status"] == "claimed"
    assert current["claim_token"] == replacement.claim_token
    assert current["claim_generation"] == 2
    assert current["acquisition_attempts"] == 1


def test_pg17_v6_failure_category_survives_reconnect_and_resolves_without_reclaim(
    isolated_pg17_schema: None,
) -> None:
    from db.refresh_run_models import (
        CycleId,
        RefreshRunFinalization,
        RefreshRunFinalState,
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

    now = datetime.now(timezone.utc)
    run_id, item_key = RunId("pg-v6-typed-terminal"), WorkItemKey("sgs_plan_unit")
    lease_until = now + timedelta(minutes=20)
    create_or_resume_run(RefreshRunStart(
        run_id, CycleId("pg-v6-typed-terminal"), True, "daily-refresh-v6", None, None,
        (RefreshWorkItemSpec(item_key, "sgs_plan_unit", 0),), now.isoformat(), lease_until.isoformat(),
    ))
    claim = claim_next_work_item(RefreshWorkItemClaimRequest(
        run_id, now.isoformat(), (now + timedelta(minutes=5)).isoformat(), item_key,
    ))
    assert claim is not None and claim.claim_token is not None
    assert complete_work_item_claim(RefreshWorkItemCompletion(
        run_id, item_key, claim.claim_token, RefreshWorkItemOutcome.FAILED,
        datetime.now(timezone.utc).isoformat(), 0, 0, "InvalidSGSPayload:188:window:outside-window",
        "invalid_schema",
    ))
    row = store._query(
        "SELECT status, last_error_category, last_error_code FROM refresh_work_items "
        "WHERE run_id = ? AND item_key = ?", (str(run_id), str(item_key)),
    )[0]
    assert row["status"] == "failed" and row["last_error_category"] == "invalid_schema"

    connection = getattr(store._pg_local, "conn", None)
    if connection is not None:
        connection.close()
        store._pg_local.conn = None
    reloaded = store._query(
        "SELECT last_error_category, last_error_code FROM refresh_work_items "
        "WHERE run_id = ? AND item_key = ?", (str(run_id), str(item_key)),
    )[0]
    assert reloaded == {
        "last_error_category": "invalid_schema", "last_error_code": row["last_error_code"],
    }
    assert claim_next_work_item(RefreshWorkItemClaimRequest(
        run_id, datetime.now(timezone.utc).isoformat(),
        (datetime.now(timezone.utc) + timedelta(minutes=5)).isoformat(), item_key,
    )) is None
    from db.refresh_runs import finalize_and_release_run

    finished = datetime.now(timezone.utc)
    assert finalize_and_release_run(RefreshRunFinalization(
        run_id, RefreshRunFinalState.PARTIAL,
        '{"status":"error","tasks":[{"name":"sgs_plan_unit","status":"error",'
        '"error_code":"InvalidSGSPayload:188:window:outside-window",'
        '"error_category":"invalid_schema"}]}',
        finished.isoformat(),
    ))


def test_pg17_sgs_plan_freeze_and_receipt_coverage_are_durable(
    isolated_pg17_schema: None,
) -> None:
    from db import store
    from db.sgs_refresh_plan import (
        CoverageReceipt,
        build_sgs_plan,
        freeze_sgs_plan,
        record_verified_coverage,
        verified_coverage,
    )

    plan = build_sgs_plan(date(2026, 10, 27), max_archive_units=1)
    run_id = "pg-sgs-plan-coverage"
    assert freeze_sgs_plan(run_id, plan) == plan
    archive = next(unit for unit in plan.units if unit.role != "recent")
    now = datetime.now(timezone.utc).isoformat()
    extraction_id = "pg-sgs-business-receipt"
    store.start_source_extraction({
        "extraction_id": extraction_id, "source": "bcb_sgs",
        "indicator": str(archive.series_code), "qualifiers_json": "{}",
        "status": "running", "network_called": 1, "started_at": now, "run_id": run_id,
    })
    store.finish_source_extraction({
        "extraction_id": extraction_id, "status": "success", "network_called": 1,
        "finished_at": now, "extracted_at": now, "source_data_date": None,
        "source_reference_min": None, "source_reference_max": None,
        "received_count": 0, "valid_count": 0, "persisted_count": 0,
        "inserted_count": 0, "updated_count": 0, "request_count": 1, "retry_count": 0,
        "error_code": None, "error_detail": None, "error_category": None,
        "retry_not_before": None,
    })
    proof = CoverageReceipt(
        operation_id="pg-sgs-operation", run_id=run_id, unit_id=archive.unit_id,
        source="bcb_sgs", series_code=archive.series_code, contract_id=archive.contract_id,
        contract_version=archive.contract_version, identity_kind=archive.identity_kind,
        scope_role=archive.role, policy_version=archive.policy_version,
        requested_start=archive.start_date, requested_end=archive.end_date,
        payload_sha256="d" * 64, observed_count=1,
        captured_at=datetime.now(timezone.utc), reference_start=archive.start_date,
        reference_end=archive.start_date,
        business_receipt_id=extraction_id,
    )
    with pytest.raises(ValueError, match="exact durable SGS unit-commit receipt"):
        record_verified_coverage(proof)
    assert verified_coverage(archive.series_code) == ()


def test_pg17_v6_refresh_status_uses_frozen_batch_inventory_and_bound_like_patterns(
    isolated_pg17_schema: None,
) -> None:
    from fastapi.testclient import TestClient

    from db.refresh_run_models import (
        CycleId,
        RefreshRunStart,
        RefreshWorkItemSpec,
        RunId,
        WorkItemKey,
    )
    from db.refresh_runs import create_or_resume_run
    from db.sgs_refresh_plan import build_sgs_plan, freeze_sgs_plan, plan_item_task_name
    from main import app

    plan = build_sgs_plan(date(2026, 10, 27))
    run_id = "pg17-v6-status-batch"
    now = datetime.now(timezone.utc)
    items = tuple(
        RefreshWorkItemSpec(WorkItemKey(plan_item_task_name(unit)),
                            plan_item_task_name(unit), ordinal)
        for ordinal, unit in enumerate(plan.units)
    )
    create_or_resume_run(RefreshRunStart(
        run_id=RunId(run_id), cycle_id=CycleId(run_id), force=False,
        manifest_version="daily-refresh-v6", derived_cutoff=None, macro_cutoff=None,
        items=items, now=now.isoformat(),
        lease_expires_at=(now + timedelta(hours=1)).isoformat(),
    ))
    freeze_sgs_plan(run_id, plan, now)

    # Exercise the real psycopg driver with bound LIKE values, including wildcard
    # characters, rather than asserting only on rendered SQL text.
    extraction_id = "pg17-v6-like-binding"
    store.start_source_extraction({
        "extraction_id": extraction_id, "source": "bcb_sgs", "indicator": "1",
        "qualifiers_json": "{}", "status": "running", "network_called": 0,
        "started_at": now.isoformat(), "run_id": run_id,
        "task_key": "evil_%_sgs_plan_fake",
    })
    assert store.source_extractions_for_refresh_run(run_id) == []
    store._execute(
        "UPDATE source_extractions SET task_key = ? WHERE extraction_id = ?",
        ("sgs_plan_fake", extraction_id),
    )
    assert len(store.source_extractions_for_refresh_run(run_id)) == 1
    assert len(store.sgs_plan_status_inventory(run_id)) == len(plan.units)

    before = store._query(
        "SELECT (SELECT COUNT(*) FROM sgs_unit_commits) AS commits, "
        "(SELECT COUNT(*) FROM sgs_coverage_receipts) AS proofs, "
        "(SELECT COUNT(*) FROM refresh_work_items WHERE run_id = ?) AS items",
        (run_id,),
    )[0]
    with TestClient(app, raise_server_exceptions=False) as client:
        response = client.get("/api/refresh-status")
    after = store._query(
        "SELECT (SELECT COUNT(*) FROM sgs_unit_commits) AS commits, "
        "(SELECT COUNT(*) FROM sgs_coverage_receipts) AS proofs, "
        "(SELECT COUNT(*) FROM refresh_work_items WHERE run_id = ?) AS items",
        (run_id,),
    )[0]
    assert response.status_code == 200
    payload = response.json()
    assert payload["sgs_incremental"]["plan_status"] == "valid"
    assert payload["sgs_incremental"]["recent"]["expected_units"] == 64
    assert payload["overall_status"] == "partial"
    assert before == after


def test_pg17_v6_status_preserves_frozen_v1_policy_and_supported_inventory(
    isolated_pg17_schema: None,
) -> None:
    from dataclasses import replace

    from fastapi.testclient import TestClient

    from db.refresh_run_models import (
        CycleId,
        RefreshRunStart,
        RefreshWorkItemSpec,
        RunId,
        WorkItemKey,
    )
    from db.refresh_runs import create_or_resume_run
    from db.sgs_refresh_plan import (
        build_sgs_plan,
        freeze_sgs_plan,
        plan_digest,
        plan_item_task_name,
        read_sgs_plan,
    )
    from main import app

    current = build_sgs_plan(date(2026, 10, 27), max_archive_units=0)
    legacy = replace(
        current,
        policy_version="sgs-coverage-policy-v1",
        units=tuple(replace(unit, policy_version="sgs-coverage-policy-v1") for unit in current.units),
    )
    run_id = "pg17-v6-frozen-v1-policy"
    now = datetime(2026, 10, 27, 8, tzinfo=timezone.utc)
    items = tuple(
        RefreshWorkItemSpec(WorkItemKey(plan_item_task_name(unit)),
                            plan_item_task_name(unit), ordinal)
        for ordinal, unit in enumerate(legacy.units)
    )
    create_or_resume_run(RefreshRunStart(
        run_id=RunId(run_id), cycle_id=CycleId(run_id), force=False,
        manifest_version="daily-refresh-v6", derived_cutoff=None, macro_cutoff=None,
        items=items, now=now.isoformat(),
        lease_expires_at=(now + timedelta(hours=1)).isoformat(),
    ))
    freeze_sgs_plan(run_id, legacy, now)
    assert read_sgs_plan(run_id) == legacy
    assert store._query(
        "SELECT plan_sha256 FROM sgs_refresh_plans WHERE run_id = ?", (run_id,),
    )[0]["plan_sha256"] == plan_digest(legacy)
    inventory = store.sgs_plan_status_inventory(run_id)
    assert len(inventory) == len(legacy.units)
    assert {row["policy_version"] for row in inventory} == {"sgs-coverage-policy-v1"}
    with TestClient(app, raise_server_exceptions=False) as client:
        response = client.get("/api/refresh-status")
    assert response.status_code == 200
    assert response.json()["sgs_incremental"]["plan_status"] == "valid"


def test_pg17_atomic_sgs_unit_commit_proof_cursor_and_rollback(
    isolated_pg17_schema: None,
) -> None:
    from db.sgs_refresh_plan import (
        SGSPlanBinding,
        SGSUnitCommitReceipt,
        build_sgs_plan,
        canonical_sgs_values,
        freeze_sgs_plan,
        plan_digest,
        reconcile_sgs_unit_commit,
        verified_coverage,
    )

    plan = build_sgs_plan(date(2026, 10, 27), max_archive_units=1)
    run_id = "pg-sgs-atomic-unit"
    freeze_sgs_plan(run_id, plan)
    unit = next(item for item in plan.units if item.role != "recent")
    value_rows = [{"data": unit.start_date.strftime("%d/%m/%Y"), "valor": "7.25"}]
    value_hash, count, reference_start, reference_end = canonical_sgs_values(unit.series_code, value_rows)
    now = datetime.now(timezone.utc)
    commit = SGSUnitCommitReceipt(
        operation_id="pg-sgs-unit-op", extraction_id="pg-sgs-unit-op", run_id=run_id,
        unit_id=unit.unit_id, plan_sha256=plan_digest(plan), series_code=unit.series_code,
        contract_id=unit.contract_id, contract_version=unit.contract_version,
        identity_kind=unit.identity_kind, scope_role=unit.role, policy_version=unit.policy_version,
        requested_start=unit.start_date, requested_end=unit.end_date,
        payload_sha256="a" * 64, canonical_value_sha256=value_hash, observed_count=count,
        reference_start=reference_start, reference_end=reference_end, captured_at=now,
    )
    store.upsert_sgs(
        unit.series_code, value_rows, first_seen_at=now.isoformat(),
        sgs_unit_commit_receipt=commit,
    )
    binding = SGSPlanBinding(run_id, plan_digest(plan), unit)
    recovered = reconcile_sgs_unit_commit(binding)
    assert recovered == commit
    assert reconcile_sgs_unit_commit(binding) == commit
    assert len(verified_coverage(unit.series_code)) == 1
    assert store.query_rows("SELECT last_series_code FROM sgs_archive_cursors WHERE source = ?", ("bcb_sgs",))[0]["last_series_code"] == unit.series_code
    repeated = replace(
        commit, operation_id="pg-sgs-unit-op-repeat", extraction_id="pg-sgs-unit-op-repeat",
        payload_sha256="c" * 64, captured_at=now + timedelta(days=1),
    )
    store.upsert_sgs(unit.series_code, value_rows, first_seen_at=(now + timedelta(days=1)).isoformat(),
                     sgs_unit_commit_receipt=repeated)
    vintages = store.query_rows(
        "SELECT COUNT(*) AS count, MIN(first_seen_at) AS first_seen FROM observation_vintages "
        "WHERE provider = ? AND series_key = ? AND reference_period = ?",
        ("bcb_sgs", unit.indicator_key, unit.start_date.isoformat()),
    )[0]
    assert vintages == {"count": 1, "first_seen": now.isoformat()}
    revised_rows = [{"data": unit.start_date.strftime("%d/%m/%Y"), "valor": "8.25"}]
    revised_hash, revised_count, revised_start, revised_end = canonical_sgs_values(unit.series_code, revised_rows)
    revision = replace(
        repeated, operation_id="pg-sgs-unit-op-revision", extraction_id="pg-sgs-unit-op-revision",
        payload_sha256="d" * 64, canonical_value_sha256=revised_hash,
        observed_count=revised_count, reference_start=revised_start, reference_end=revised_end,
        captured_at=now + timedelta(days=2),
    )
    store.upsert_sgs(unit.series_code, revised_rows, first_seen_at=revision.captured_at.isoformat(),
                     sgs_unit_commit_receipt=revision)
    assert store.query_rows(
        "SELECT COUNT(*) AS count FROM observation_vintages WHERE provider = ? "
        "AND series_key = ? AND reference_period = ?",
        ("bcb_sgs", unit.indicator_key, unit.start_date.isoformat()),
    )[0]["count"] == 2

    # A receipt insert failure aborts business rows, vintage, and source origins together.
    rollback_unit = next(item for item in plan.units if item.role == "recent")
    rollback_records = [{"data": rollback_unit.start_date.strftime("%d/%m/%Y"), "valor": "8.5"}]
    rollback_hash, rollback_count, rollback_start, rollback_end = canonical_sgs_values(rollback_unit.series_code, rollback_records)
    rollback_commit = SGSUnitCommitReceipt(
        operation_id="pg-sgs-rollback-op", extraction_id="pg-sgs-rollback-op", run_id=run_id,
        unit_id=rollback_unit.unit_id, plan_sha256=plan_digest(plan), series_code=rollback_unit.series_code,
        contract_id=rollback_unit.contract_id, contract_version=rollback_unit.contract_version,
        identity_kind=rollback_unit.identity_kind, scope_role=rollback_unit.role,
        policy_version=rollback_unit.policy_version, requested_start=rollback_unit.start_date,
        requested_end=rollback_unit.end_date, payload_sha256="b" * 64,
        canonical_value_sha256=rollback_hash, observed_count=rollback_count,
        reference_start=rollback_start, reference_end=rollback_end, captured_at=now,
    )
    store._execute(
        "CREATE FUNCTION reject_sgs_unit_commit() RETURNS trigger LANGUAGE plpgsql "
        "AS $$ BEGIN RAISE EXCEPTION 'injected unit receipt failure'; END $$"
    )
    store._execute("CREATE TRIGGER reject_sgs_unit_commit BEFORE INSERT ON sgs_unit_commits "
                   "FOR EACH ROW EXECUTE FUNCTION reject_sgs_unit_commit()")
    origin = (
        "rollback-origin", rollback_commit.extraction_id, "bcb_sgs", rollback_unit.contract_id,
        '{"reference_date":"' + rollback_start.isoformat() + '"}', rollback_start.isoformat(),
        "c" * 64, "primary", None, "d" * 64, None, now.isoformat(),
    )
    with pytest.raises(psycopg.errors.RaiseException, match="injected unit receipt failure"):
        store.upsert_sgs(
            rollback_unit.series_code, rollback_records, origin_rows=[origin],
            sgs_unit_commit_receipt=rollback_commit,
        )
    assert store.query_rows(
        "SELECT 1 FROM sgs WHERE series_code = ? AND date = ?",
        (rollback_unit.series_code, rollback_start.isoformat()),
    ) == []
    assert store.query_rows(
        "SELECT 1 FROM observation_vintages WHERE series_key = ? AND reference_period = ?",
        (rollback_unit.indicator_key, rollback_start.isoformat()),
    ) == []
    assert store.query_rows("SELECT 1 FROM source_observation_origins WHERE extraction_id = ?", (rollback_commit.extraction_id,)) == []
    assert store.query_rows("SELECT 1 FROM sgs_unit_commits WHERE operation_id = ?", (rollback_commit.operation_id,)) == []
    assert store._query(
        "SELECT status FROM sgs_refresh_plan_units WHERE run_id = ? AND unit_id = ?",
        (run_id, unit.unit_id),
    )[0]["status"] == "verified"


def test_pg17_api_v6_freeze_rollback_resume_and_mock_http_dispatch(
    isolated_pg17_schema: None, monkeypatch: pytest.MonkeyPatch,
) -> None:
    from starlette.testclient import TestClient

    import db.refresh_run_lifecycle as lifecycle
    import provider_http
    import refresh_run_execution
    from datafetchers import bcb_sgs
    from db import store
    from db.refresh_run_models import CycleId, RunId
    from db.sgs_refresh_plan import read_sgs_plan
    from main import app
    from refresh import RefreshDependencies
    from refresh_run_execution import RefreshRunStartRequest, start_refresh_run

    original_insert = lifecycle.insert_sgs_plan_rows

    def insert_then_fail(connection, run_id, plan, created_at):
        original_insert(connection, run_id, plan, created_at)
        if run_id == "pg-v6-atomic-failure":
            raise RuntimeError("forced after plan rows")

    monkeypatch.setattr(lifecycle, "insert_sgs_plan_rows", insert_then_fail)
    with pytest.raises(RuntimeError, match="forced after plan rows"):
        start_refresh_run(
            RefreshRunStartRequest(RunId("pg-v6-atomic-failure"), CycleId("pg-v6-atomic-failure"), True),
            RefreshDependencies(),
        )
    for table in ("refresh_runs", "refresh_work_items", "sgs_refresh_plans", "refresh_leases"):
        assert store.query_rows(f"SELECT 1 AS present FROM {table} WHERE " + (
            "owner = ?" if table == "refresh_leases" else "run_id = ?"
        ), ("pg-v6-atomic-failure",)) == []

    class Handler(BaseHTTPRequestHandler):
        calls = 0

        def do_GET(self) -> None:
            type(self).calls += 1
            query = parse_qs(urlparse(self.path).query)
            row_date = query.get("dataFinal", ["27/10/2026"])[0]
            body = json.dumps([{"data": row_date, "valor": "1.25"}]).encode()
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, _format: str, *_args: object) -> None:
            return

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    server.daemon_threads = True
    thread = Thread(target=server.serve_forever, daemon=True)
    thread.start()
    monkeypatch.setattr(bcb_sgs, "BCB_SGS_BASE", f"http://127.0.0.1:{server.server_port}/series/{{code}}")
    monkeypatch.setattr(provider_http, "require_allowed_provider_url", lambda url: url)
    dependencies = RefreshDependencies(fetch_sgs_window=bcb_sgs.fetch_sgs_window)
    monkeypatch.setattr(refresh_run_execution, "_dependencies", lambda _value: dependencies)
    run_id = "pg-v6-api-frozen"
    try:
        with TestClient(app) as client:
            response = client.post(
                "/api/refresh-runs", json={"run_id": run_id, "cycle_id": run_id, "force": True}
            )
            assert response.status_code == 200
            payload = response.json()
            assert len(payload["items"]) == 75
            assert all(item["task"].startswith("sgs_plan_") for item in payload["items"][:68])
            assert not any(
                item["task"].startswith("sgs_") and not item["task"].startswith("sgs_plan_")
                for item in payload["items"]
            )
            frozen = read_sgs_plan(run_id)
            assert frozen is not None and len(frozen.units) == 68
            assert store.query_rows(
                "SELECT COUNT(*) AS count FROM sgs_refresh_plan_units WHERE run_id = ?", (run_id,)
            )[0]["count"] == 68

            def unexpected_replan(_as_of):
                raise AssertionError("resume must not regenerate its frozen SGS plan")

            monkeypatch.setattr(refresh_run_execution, "build_next_sgs_plan", unexpected_replan)
            resumed = client.post(
                "/api/refresh-runs", json={"run_id": run_id, "cycle_id": run_id, "force": True}
            )
            assert resumed.status_code == 200
            assert resumed.json()["items"] == payload["items"]
            first_item = payload["items"][0]
            advanced = client.post(
                f"/api/refresh-runs/{run_id}/advance",
                json={"expected_item_key": first_item["item_key"]},
            )
            assert advanced.status_code == 200
            assert Handler.calls == 1
            extraction = store.query_rows(
                "SELECT status, network_called FROM source_extractions WHERE run_id = ? "
                "ORDER BY started_at DESC LIMIT 1", (run_id,),
            )[0]
            assert extraction["status"] == "success"
            assert extraction["network_called"] == 1
            code = frozen.units[0].series_code
            stored = store.query_rows("SELECT value FROM sgs WHERE series_code = ? LIMIT 1", (code,))
            assert stored and float(stored[0]["value"]) == 1.25
            commits = store.query_rows(
                "SELECT run_id, unit_id, series_code, observed_count FROM sgs_unit_commits "
                "WHERE run_id = ?", (run_id,),
            )
            assert commits == [{
                "run_id": run_id, "unit_id": frozen.units[0].unit_id,
                "series_code": frozen.units[0].series_code, "observed_count": 1,
            }]
            commit_count_before_status = store.query_rows(
                "SELECT COUNT(*) AS count FROM sgs_unit_commits WHERE run_id = ?", (run_id,)
            )[0]["count"]
            status = client.get("/api/refresh-status")
            assert status.status_code == 200
            status_payload = status.json()
            assert status_payload["overall_status"] == "partial"
            assert status_payload["sgs_incremental"]["recent"]["network_refreshed_units"] == 1
            projected = next(
                task for task in status_payload["tasks"]
                if task["task"] == "sgs_cambio"
            )
            assert projected["series_network_refreshed"] == 1
            assert projected["series_available"] >= 1
            assert projected["last_data_captured_at"] is not None
            assert Handler.calls == 1
            assert store.query_rows(
                "SELECT COUNT(*) AS count FROM sgs_unit_commits WHERE run_id = ?", (run_id,)
            )[0]["count"] == commit_count_before_status
    finally:
        server.shutdown()
        thread.join(timeout=5)
        server.server_close()


@pytest.mark.parametrize("retry_after_kind", ["seconds", "http_date", "missing", "invalid"])
def test_pg17_actual_sgs_429_route_defers_with_shared_retry_advice(
    isolated_pg17_schema: None, retry_after_kind: str, monkeypatch: pytest.MonkeyPatch,
) -> None:
    from starlette.testclient import TestClient

    import provider_http
    import refresh_run_execution
    from datafetchers import bcb_sgs
    from main import app
    from refresh import RefreshDependencies

    now = datetime.now(timezone.utc)
    retry_header = {
        "seconds": "3600",
        "http_date": format_datetime(now + timedelta(hours=1), usegmt=True),
        "missing": None,
        "invalid": "not-a-date",
    }[retry_after_kind]

    class Handler(BaseHTTPRequestHandler):
        calls = 0

        def do_GET(self) -> None:
            type(self).calls += 1
            self.send_response(429)
            if retry_header is not None:
                self.send_header("Retry-After", retry_header)
            self.send_header("Content-Length", "0")
            self.end_headers()

        def log_message(self, _format: str, *_args: object) -> None:
            return

    Handler.calls = 0
    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    server.daemon_threads = True
    thread = Thread(target=server.serve_forever, daemon=True)
    thread.start()
    monkeypatch.setattr(bcb_sgs, "BCB_SGS_BASE", f"http://127.0.0.1:{server.server_port}/series/{{code}}")
    monkeypatch.setattr(provider_http, "require_allowed_provider_url", lambda url: url)
    monkeypatch.setattr(
        refresh_run_execution,
        "_dependencies",
        lambda _value: RefreshDependencies(fetch_sgs_window=bcb_sgs.fetch_sgs_window),
    )
    run_id = f"pg-429-{retry_after_kind}"
    try:
        with TestClient(app) as client:
            started = client.post(
                "/api/refresh-runs", json={"run_id": run_id, "cycle_id": run_id, "force": True}
            )
            item = started.json()["items"][0]
            response = client.post(
                f"/api/refresh-runs/{run_id}/advance", json={"expected_item_key": item["item_key"]}
            )
            first_row = store._query(
                "SELECT status, attempt, claim_generation, acquisition_attempts, next_attempt_at, "
                "deferral_reason, claim_token FROM refresh_work_items WHERE run_id = ? AND item_key = ?",
                (run_id, item["item_key"]),
            )[0]
            extraction = store._query(
                "SELECT error_category, request_count, persisted_count FROM source_extractions "
                "WHERE run_id = ? ORDER BY started_at DESC LIMIT 1", (run_id,),
            )[0]
            store._execute(
                "UPDATE refresh_work_items SET next_attempt_at = ? WHERE run_id = ? AND item_key = ?",
                ("2000-01-01T00:00:00+00:00", run_id, item["item_key"]),
            )
            store._execute(
                "UPDATE source_access_state SET state = 'closed', retry_not_before = NULL, "
                "opened_until = NULL, probe_lease_until = NULL WHERE scope_id = ?",
                ("bcb_sgs:primary:primary",),
            )
            second = client.post(
                f"/api/refresh-runs/{run_id}/advance", json={"expected_item_key": item["item_key"]}
            )
            second_row = store._query(
                "SELECT status, attempt, claim_generation, acquisition_attempts, next_attempt_at "
                "FROM refresh_work_items WHERE run_id = ? AND item_key = ?",
                (run_id, item["item_key"]),
            )[0]
            third = client.post(
                f"/api/refresh-runs/{run_id}/advance", json={"expected_item_key": item["item_key"]}
            )
            store._execute(
                "UPDATE refresh_work_items SET status = 'skipped', finished_at = ? "
                "WHERE run_id = ? AND item_key <> ?",
                (datetime.now(timezone.utc).isoformat(), run_id, item["item_key"]),
            )
            finalized = client.post(f"/api/refresh-runs/{run_id}/finalize")
            final_run = finalized.json()["run"]
            final_tasks = json.loads(final_run["final_result"])["tasks"]
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=1)

    assert started.status_code == response.status_code == second.status_code == third.status_code == 200
    assert first_row["status"] == "pending"
    assert first_row["deferral_reason"] == "retry_after"
    assert first_row["claim_token"] is None
    response_item = next(
        value for value in response.json()["items"] if value["item_key"] == item["item_key"]
    )
    assert response_item["last_error_category"] == "rate_limited"
    assert datetime.fromisoformat(response.json()["run"]["next_due_at"]) >= datetime.fromisoformat(
        first_row["next_attempt_at"]
    )
    assert response.json()["run"]["due_work_item_key"] != item["item_key"]
    assert first_row["attempt"] == first_row["claim_generation"] == 1
    assert first_row["acquisition_attempts"] == 1
    minimum_cooldown = timedelta(minutes=59) if retry_after_kind in {"seconds", "http_date"} else timedelta(minutes=4)
    assert datetime.fromisoformat(first_row["next_attempt_at"]) >= now + minimum_cooldown
    assert extraction["error_category"] == "rate_limited"
    assert extraction["request_count"] == 1
    assert extraction["persisted_count"] in (None, 0)
    assert second_row["status"] == "pending"
    assert second_row["attempt"] == second_row["claim_generation"] == 2
    assert second_row["acquisition_attempts"] == 2
    assert Handler.calls == 2
    assert store._query("SELECT * FROM sgs WHERE series_code = ?", (432,)) == []
    assert store.get_meta("sgs_last_refresh_432") is None
    assert finalized.status_code == 200
    assert final_run["state"] == "partial"
    assert final_run["lease_expires_at"] is None
    deferred_task = next(task for task in final_tasks if task["name"] == item["task"])
    assert deferred_task["acquisition_status"] == "deferred"
    assert deferred_task["error_category"] == "rate_limited"
    assert deferred_task["retry_not_before"] == second_row["next_attempt_at"]
    assert deferred_task["deferral_reason"] == "retry_after"
    assert store.get_meta("last_successful_update") is None


def test_pg17_tesouro_commit_then_ledger_failure_retains_known_counts_without_redownload(
    isolated_pg17_schema: None,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from source_ingestion import SourceOperationError

    today = datetime.now(timezone.utc).date()
    body = (
        "Tipo Titulo;Data Vencimento;Data Base;Taxa Compra Manha;Taxa Venda Manha;"
        "PU Compra Manha;PU Venda Manha;PU Base Manha\n"
        f"Tesouro Prefixado;{today.replace(year=today.year + 5):%d/%m/%Y};"
        f"{today:%d/%m/%Y};11,1;11,2;900,1;901,2;899,0\n"
    ).encode()
    response = MagicMock(content=body, headers={"content-length": str(len(body))})
    monkeypatch.setattr(
        tesouro_direto, "get_provider_response_bounded", MagicMock(return_value=response)
    )
    monkeypatch.setattr(
        "source_ingestion.finish_source_extraction",
        MagicMock(side_effect=RuntimeError("ledger unavailable")),
    )
    token = set_refresh_context("pg-tesouro-log-failure", "treasury", "pg-tesouro-cycle")
    try:
        with pytest.raises(SourceOperationError) as raised:
            tesouro_direto.refresh_treasury_data()
    finally:
        reset_refresh_context(token)

    assert raised.value.stage == "logging"
    assert raised.value.persisted_count == raised.value.committed_count == 1
    assert store._query("SELECT * FROM treasury_cohort_commits")
    assert len(store._query("SELECT * FROM treasury")) == 1
    assert tesouro_direto.get_provider_response_bounded.call_count == 1
