from __future__ import annotations

import os
from collections.abc import Iterator
from datetime import datetime, timezone
from threading import local
from unittest.mock import MagicMock, patch
from uuid import uuid4

import psycopg
import pytest
from psycopg.conninfo import conninfo_to_dict, make_conninfo

from datafetchers import focus
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
    assert versions[-1]["version"] == "013_source_extractions"
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
