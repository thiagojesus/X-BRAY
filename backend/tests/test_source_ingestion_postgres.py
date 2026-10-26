from __future__ import annotations

import os
from collections.abc import Iterator
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone
from threading import Barrier, local
from unittest.mock import MagicMock, patch
from uuid import uuid4

import psycopg
import pytest
from psycopg.conninfo import conninfo_to_dict, make_conninfo

from datafetchers import b3_di, bcb_sgs, focus
from db import store
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
        cursor.execute("SHOW server_version_num")
        version = int(cursor.fetchone()[0])
        if version < 170000 or version >= 180000:
            pytest.fail("integration fixture requires PostgreSQL 17")
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
    assert versions[-1]["version"] == "016_candidate_acquisition"
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
            admitted, _, half_open = store.claim_source_access_probe(
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
