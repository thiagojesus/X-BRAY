from __future__ import annotations

import hashlib
import os
from calendar import monthrange
from collections.abc import Iterator
from datetime import date, datetime, timedelta, timezone
from threading import local
from unittest.mock import patch
from uuid import uuid4

import psycopg
import pytest
from fastapi.testclient import TestClient
from psycopg.conninfo import conninfo_to_dict, make_conninfo

from db import store
from derived.models import DerivedInput, DerivedResult, SnapshotOrigin
from derived.repository import (
    insert_snapshot,
    query_latest_snapshots,
    query_snapshot_history,
    refresh_derived_snapshots,
)
from main import app

AS_OF = datetime(2026, 10, 21, 12, tzinfo=timezone.utc)
CAPTURED_AT = datetime(2026, 10, 20, 12, tzinfo=timezone.utc)
CURRENT_AVAILABLE_AT = datetime(2026, 10, 19, 12, tzinfo=timezone.utc)
FOCUS_KEY = "IPCA_12M_S_BASE0"


@pytest.fixture
def isolated_postgres_schema(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
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
        schema = f"xbray_real_rate_{uuid4().hex}"
        cursor.execute("CREATE SCHEMA \"" + schema + "\"")
    isolated_url = make_conninfo(database_url, options=f"-c search_path={schema}")
    monkeypatch.setattr(store, "DATABASE_URL", isolated_url)
    monkeypatch.setattr(store, "_pg_local", local())
    store.init_db()
    try:
        yield
    finally:
        connection = getattr(store._pg_local, "conn", None)
        if connection is not None:
            connection.close()
        with psycopg.connect(database_url, autocommit=True) as admin, admin.cursor() as cursor:
            cursor.execute("DROP SCHEMA \"" + schema + "\" CASCADE")
        monkeypatch.setattr(store, "_pg_local", local())


def _month_end(index: int) -> date:
    year, month_zero = divmod(index, 12)
    year += month_zero // 12
    month = month_zero % 12 + 1
    return date(year, month, monthrange(year, month)[1])


def _seed_focus(reference: date, median: float) -> None:
    with patch(
        "db.store._utc_now", return_value=CAPTURED_AT.isoformat()
    ):
        store.upsert_focus(
            FOCUS_KEY,
            [{
                "Indicador": "IPCA",
                "Data": reference.isoformat(),
                "Mediana": median,
                "Suavizada": "S",
                "baseCalculo": 0,
            }],
        )


def _seed_current_inputs() -> float:
    reference = date(2026, 10, 16)
    expected_inflation = 4.63
    with patch("db.store._utc_now", return_value=CURRENT_AVAILABLE_AT.isoformat()):
        store.upsert_focus(
            FOCUS_KEY,
            [{
                "Indicador": "IPCA",
                "Data": reference.isoformat(),
                "Mediana": expected_inflation,
                "Suavizada": "S",
                "baseCalculo": 0,
            }],
        )
    with patch(
        "db.store._utc_now", return_value=CURRENT_AVAILABLE_AT.isoformat()
    ):
        store.upsert_b3_di([{
            "trade_date": reference.isoformat(),
            "maturity": (reference + timedelta(days=365)).isoformat(),
            "symbol": "DI1_CURRENT",
            "rate": 13.0,
        }])
    return expected_inflation


def _seed_monthly_retro_history(
    *, month_count: int = 24, conflict_index: int | None = None
) -> None:
    for index in range(month_count):
        reference = _month_end(2024 * 12 + index)
        symbol = f"DI1_{index:02d}"
        maturity = reference + timedelta(days=365)
        fin_id = f"FIN-{index:02d}"
        isin = f"BR-ISIN-{index:02d}"
        digest = hashlib.sha256(f"retro-price-{index}".encode()).hexdigest()
        store.execute_change(
            "INSERT INTO retrospective_di_observations "
            "(trade_date,symbol,maturity,rate,source_hash,fetched_at,revision,source_basis,source_name,"
            "source_member,maturity_basis,security_id) VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
            (
                reference.isoformat(), symbol, maturity.isoformat(), 10.0 + index / 10,
                digest, CAPTURED_AT.isoformat(), f"rev-{index}", "retrospective_reference",
                "price-report.xml", f"member-{index}", "b3_instrument_snapshot_v1", fin_id,
            ),
        )
        store.execute_change(
            "INSERT INTO b3_di_instrument_snapshots "
            "(snapshot_date,report_date,symbol,security_id,fin_instrm_id,maturity,trading_start,trading_end,"
            "source_hash,source_name,message_definition,message_id,captured_at,available_at,source_basis) "
            "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (
                CAPTURED_AT.date().isoformat(), CAPTURED_AT.date().isoformat(), symbol, isin,
                fin_id, maturity.isoformat(), None, None, digest, "instrument-report.xml",
                "bvmf.100.02", f"message-{index}", CAPTURED_AT.isoformat(),
                CAPTURED_AT.isoformat(), "b3_instrument_snapshot_v1",
            ),
        )
        if index == conflict_index:
            store.execute_change(
                "INSERT INTO retrospective_di_price_identities "
                "(trade_date,symbol,source_hash,source_member,security_id,captured_at,source_basis) "
                "VALUES (?,?,?,?,?,?,?)",
                (
                    reference.isoformat(), symbol, digest, f"member-{index}", "CONFLICTING-ID",
                    CAPTURED_AT.isoformat(), "b3_price_identity_v1",
                ),
            )
        _seed_focus(reference, 3.0 + index / 20)


def _seed_production_collision() -> date:
    reference = _month_end(2024 * 12)
    snapshot_time = datetime(2024, 2, 1, 12, tzinfo=timezone.utc)
    inputs = (
        DerivedInput(
            key="nominal_rate",
            value=105.0,
            unit="pct_annual",
            reference_period=reference.isoformat(),
            available_at=snapshot_time,
            source="production fixture DI",
        ),
        DerivedInput(
            key="expected_inflation",
            value=2.5,
            unit="pct_annual",
            reference_period=reference.isoformat(),
            available_at=snapshot_time,
            source="production fixture Focus",
        ),
    )
    result = DerivedResult(
        key="juro_real_ex_ante_1y",
        value=100.0,
        unit="pct_annual",
        as_of=snapshot_time,
        methodology_version="1.0.0",
        formula="historical-production-snapshot-fixture",
        inputs=inputs,
        reason=None,
        reference_date=reference.isoformat(),
    )
    assert insert_snapshot(
        result,
        calculation_cycle_id="historical-production-collision-fixture",
        origin=SnapshotOrigin.PRODUCTION_REFRESH,
        created_at=snapshot_time,
    )
    return reference


def test_postgres_refresh_loads_monthly_di_focus_persists_and_serves_classification(
    isolated_postgres_schema: None,
) -> None:
    _seed_monthly_retro_history()
    production_reference = _seed_production_collision()
    expected_focus = _seed_current_inputs()
    original_value = ((1 + 13.0 / 100) / (1 + expected_focus / 100) - 1) * 100

    assert store.uses_postgres()
    assert refresh_derived_snapshots(AS_OF, "pg-real-rate-retro") == 1
    result = query_latest_snapshots()["juro_real_ex_ante_1y"].result

    assert result.value == pytest.approx(original_value)
    assert result.classification_history_count == 25
    assert result.classification_history_basis == (
        "retrospective_reference_monthly_plus_production_forward_captured_monthly"
    )
    assert result.classification_history_available_at == CAPTURED_AT
    assert result.historical_percentile == pytest.approx(94.0)
    history = query_snapshot_history("juro_real_ex_ante_1y")
    collision = next(
        item.result for item in history if item.result.reference_date == production_reference.isoformat()
    )
    assert collision.value == 100.0
    with store._get_pg_conn().cursor() as cursor:
        cursor.execute(
            "SELECT pg_typeof(rate)::text FROM b3_di WHERE symbol = %s",
            ("DI1_CURRENT",),
        )
        assert cursor.fetchone()["pg_typeof"] == "double precision"
        cursor.execute("SELECT rate FROM b3_di WHERE symbol = %s", ("DI1_CURRENT",))
        assert isinstance(cursor.fetchone()["rate"], float)

    with TestClient(app) as client:
        response = client.get("/api/derivados/juro-real-ex-ante-1y")
    assert response.status_code == 200
    data = response.json()["data"]
    assert data["value"] == pytest.approx(original_value)
    assert data["classification_history_count"] == 25
    assert data["classification_history_basis"] == result.classification_history_basis
    assert data["classification_history_available_at"] == CAPTURED_AT.isoformat()


def test_postgres_source_conflict_fails_closed_and_historical_cutoff_does_not_backfill(
    isolated_postgres_schema: None,
) -> None:
    _seed_monthly_retro_history(conflict_index=0)
    _seed_current_inputs()

    assert refresh_derived_snapshots(AS_OF, "pg-real-rate-conflict") == 1
    latest = query_latest_snapshots()["juro_real_ex_ante_1y"].result
    assert latest.value is not None
    assert latest.classification_history_count == 24
    assert latest.classification_reason is None

    earlier_cutoff = datetime(2026, 10, 19, 18, tzinfo=timezone.utc)
    assert refresh_derived_snapshots(earlier_cutoff, "pg-real-rate-earlier-cutoff") == 1
    older = next(
        snapshot.result
        for snapshot in query_snapshot_history("juro_real_ex_ante_1y")
        if snapshot.result.as_of == earlier_cutoff
    )
    assert older.value is not None
    assert older.classification_reason == "insufficient_history"
    assert older.classification_history_count == 1
    assert older.classification_history_basis == "production_forward_captured"
    assert older.classification_history_available_at is None
    latest = query_latest_snapshots()["juro_real_ex_ante_1y"].result
    assert latest.as_of == AS_OF
    assert latest.value is not None
    assert latest.classification_reason is None


@pytest.mark.parametrize(
    ("month_count", "expected_observations", "expected_reason"),
    [(22, 23, "insufficient_history"), (23, 24, None)],
)
def test_postgres_monthly_percentile_threshold_is_23_pending_24_eligible(
    isolated_postgres_schema: None,
    month_count: int,
    expected_observations: int,
    expected_reason: str | None,
) -> None:
    _seed_monthly_retro_history(month_count=month_count)
    _seed_current_inputs()

    assert refresh_derived_snapshots(AS_OF, f"pg-threshold-{month_count}") == 1
    result = query_latest_snapshots()["juro_real_ex_ante_1y"].result

    assert result.value is not None
    assert result.classification_history_count == expected_observations
    assert result.classification_reason == expected_reason
    if expected_reason is None:
        assert result.historical_percentile is not None
        assert result.classification is not None


def test_postgres_source_audit_snapshot_queries_all_bounded_tables_read_only(
    isolated_postgres_schema: None,
) -> None:
    from db.store import source_audit_rows
    from routes.source_audit import _decode_rows

    store.upsert_sgs(433, [{"data": "01/09/2026", "valor": "0.82"}])
    store.upsert_focus("IPCA", [{"Data": "2026-10-20", "DataReferencia": "2027", "Mediana": 4.1}])
    store.upsert_anbima("ima_b", [{"Data de Referência": "2026-10-20", "IMA-B": 100.1}])

    first = source_audit_rows()
    decoded = _decode_rows(first)
    second = source_audit_rows()

    assert first["sgs"][0]["series_code"] == 433
    assert first["focus_vintages"][0]["provider"] == "focus"
    assert first["anbima"][0]["sheet_name"] == "ima_b"
    assert decoded["anbima"][0]["data_json"]["IMA-B"] == 100.1
    assert first["audit_limits"] == [{"focus_omitted": 0, "anbima_omitted": 0}]
    assert second["sgs"] == first["sgs"]
    assert second["anbima"] == first["anbima"]
