from __future__ import annotations

import csv
import hashlib
import io
import json
import os
import time
from collections.abc import Iterator
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch
from urllib.parse import urlsplit

import psycopg
import pytest

from datafetchers.tesouro_bonds import _bond_code
from datafetchers.tesouro_direto import refresh_treasury_data
from db import store
from db.store import TreasuryCohortIntent, TreasuryCohortReceipt
from db.treasury_store import (
    TreasuryCsvError,
    persist_treasury_rows,
    prepare_treasury_rows,
    validate_treasury_csv,
)
from source_ingestion import SourceOperationError

FIELDS = (
    "Tipo Titulo", "Data Vencimento", "Data Base", "Taxa Compra Manha",
    "Taxa Venda Manha", "PU Compra Manha", "PU Venda Manha", "PU Base Manha",
)


def _csv_payload(*, rows: int = 1, malformed_at: int | None = None) -> bytes:
    lines = [";".join(FIELDS)]
    base = date(2026, 10, 26)
    for index in range(rows):
        bond_index = index % 1800
        vintage_day = index // 1800
        family = "Tesouro Prefixado" if bond_index % 2 == 0 else "Tesouro IPCA+ com Juros Semestrais"
        kind = f"{family} {bond_index:04d}"
        maturity = (date(2030, 1, 1) + timedelta(days=bond_index * 7)).strftime("%d/%m/%Y")
        base_text = (base - timedelta(days=vintage_day)).strftime("%d/%m/%Y")
        values = [kind, maturity, base_text, "11,25", "11,30", "1.234,56", "1.235,12", "1.234,00"]
        if malformed_at == index:
            values[1] = "bad-date"
        lines.append(";".join(values))
    return ("\ufeff" + "\n".join(lines) + "\n").encode("utf-8")


@pytest.fixture
def sqlite_db(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    if store._conn is not None:
        store._conn.close()
    store._conn = None
    monkeypatch.setattr(store, "DATABASE_URL", "")
    monkeypatch.setattr(store, "DB_PATH", tmp_path / "treasury-bounded.db")
    store.init_db()
    yield
    if store._conn is not None:
        store._conn.close()
    store._conn = None


def _prepared(payload: bytes, extraction_id: str) -> tuple[list[tuple[int, str, str]], TreasuryCohortReceipt]:
    parsed, payload_hash = validate_treasury_csv(payload)
    rows = prepare_treasury_rows(parsed)
    bases = [row["base_date"] for row in parsed]
    receipt = TreasuryCohortReceipt(
        extraction_id, payload_hash, len(rows), min(bases), max(bases),
        datetime.now(timezone.utc).isoformat(),
    )
    return rows, receipt


def _seed_old_sqlite_cohort() -> None:
    assert store.replace_treasury_rows([{
        "_bond_code": 1, "Data Base": "01/09/2026", "Tipo Titulo": "Tesouro Prefixado old",
        "Data Vencimento": "01/01/2030",
    }]) == 1
    store.set_meta("td_last_refresh", "old-freshness")


def test_csv_validation_preserves_empty_quote_markers_and_rejects_incomplete_cohort() -> None:
    payload = _csv_payload()
    rows, digest = validate_treasury_csv(payload)
    assert rows[0]["values"]["PU Compra Manha"] == "1.234,56"
    assert digest == hashlib.sha256(payload).hexdigest()

    for bad_payload, length in (
        (b"", None), (b"<html>bad</html>", None), (_csv_payload(malformed_at=0), None),
        (payload, str(len(payload) + 1)),
        (b";".join(field.encode() for field in FIELDS) + b"\n" + b"Tesouro;01/01/2030", None),
    ):
        with pytest.raises(TreasuryCsvError):
            validate_treasury_csv(bad_payload, content_length=length)


def test_csv_rejects_future_base_but_accepts_future_maturity() -> None:
    lines = _csv_payload().decode("utf-8-sig").splitlines()
    fields = lines[1].split(";")
    fields[1] = "01/01/2099"
    fields[2] = "28/10/2026"
    payload = (";".join(lines[0].split(";")) + "\n" + ";".join(fields) + "\n").encode()
    with pytest.raises(TreasuryCsvError, match="future quote base date"):
        validate_treasury_csv(payload, capture_date=date(2026, 10, 27))
    fields[2] = "27/10/2026"
    valid = (lines[0] + "\n" + ";".join(fields) + "\n").encode()
    rows, _digest = validate_treasury_csv(valid, capture_date=date(2026, 10, 27))
    assert rows[0]["base_date"] == "2026-10-27"


def test_v6_receipt_lookup_requires_exact_stable_intent(sqlite_db: None) -> None:
    rows, receipt = _prepared(_csv_payload(rows=2), "intent-extraction")
    intent = TreasuryCohortIntent(
        "run-intent", "tesouro", "contract-v1", "official-dataset",
        "a" * 64, "",
    )
    committed = persist_treasury_rows(
        rows, extraction_id=receipt.extraction_id,
        source_payload_sha256=receipt.source_payload_sha256,
        captured_at=receipt.captured_at,
        reference_min=receipt.reference_min,
        reference_max=receipt.reference_max,
        deadline_at=time.monotonic() + 10, intent=intent,
    )
    found = store.treasury_cohort_commit_for_intent(intent)
    assert found is not None
    assert found[0] == committed
    assert store.treasury_cohort_commit_for_intent(intent._replace(run_id="other-run")) is None


def test_v6_treasury_producer_replays_same_run_intent_without_second_http(
    sqlite_db: None,
) -> None:
    from db.refresh_run_models import (
        CycleId,
        RefreshRunStart,
        RefreshWorkItemSpec,
        RunId,
        WorkItemKey,
    )
    from db.refresh_runs import create_or_resume_run
    from source_ingestion import reset_refresh_context, set_refresh_context

    run_id = RunId("treasury-v6-replay")
    now = datetime.now(timezone.utc)
    create_or_resume_run(RefreshRunStart(
        run_id=run_id, cycle_id=CycleId(str(run_id)), force=True,
        manifest_version="daily-refresh-v6", derived_cutoff=None, macro_cutoff=None,
        items=(RefreshWorkItemSpec(WorkItemKey("tesouro"), "tesouro", 0),),
        now=now.isoformat(), lease_expires_at=(now + timedelta(minutes=30)).isoformat(),
    ))
    token = set_refresh_context(str(run_id), "tesouro", str(run_id))
    response = SimpleNamespace(
        content=_csv_payload(rows=2),
        headers={"content-length": str(len(_csv_payload(rows=2)))},
    )
    try:
        with patch("datafetchers.tesouro_direto.get_provider_response_bounded", return_value=response) as fetch:
            first = refresh_treasury_data()
            captured = store.get_meta("td_last_refresh")
            second = refresh_treasury_data()
        assert fetch.call_count == 1
    finally:
        reset_refresh_context(token)

    assert first == {"rows": 2}
    assert second["receipt_replay"] is True
    assert store.get_meta("td_last_refresh") == captured
    extraction = store._query(
        "SELECT network_called, received_count, persisted_count, request_count "
        "FROM source_extractions WHERE run_id = ? ORDER BY started_at DESC LIMIT 1",
        (str(run_id),),
    )[0]
    assert (
        extraction["network_called"], extraction["received_count"],
        extraction["persisted_count"], extraction["request_count"],
    ) == (0, 0, 0, 0)


def test_treasury_extraction_persists_only_sanitized_bounded_trace(sqlite_db: None) -> None:
    from ingestion_policy import current_retry_budget

    body = _csv_payload(rows=1)
    secret = "Bearer secret-test-token"
    response = SimpleNamespace(content=body, headers={"content-length": str(len(body))})

    def fetched(*_args, **_kwargs):
        budget = current_retry_budget()
        assert budget is not None
        budget.record_http_trace(
            route_id="tesouro_daily_csv",
            allowed_host="www.tesourotransparente.gov.br",
            attempt_ordinal=1, hop_ordinal=1, status_code=200,
            dispatch_started_at="2026-10-27T12:00:00+00:00",
            dispatch_finished_at="2026-10-27T12:00:01+00:00",
            bytes_received=len(body), dispatch_kind="initial",
        )
        return response

    with patch("datafetchers.tesouro_direto.get_provider_response_bounded", side_effect=fetched):
        refresh_treasury_data()

    trace_json = store._query(
        "SELECT request_trace_json FROM source_extractions ORDER BY started_at DESC LIMIT 1"
    )[0]["request_trace_json"]
    trace = json.loads(trace_json)
    assert len(trace) == 1
    assert trace[0]["route_id"] == "tesouro_daily_csv"
    assert trace[0]["bytes_received"] == len(body)
    assert "Authorization" not in trace_json
    assert "Bearer" not in trace_json
    assert body.decode("utf-8-sig") not in trace_json
    assert secret not in trace_json


@pytest.mark.parametrize(
    "payload",
    [
        b"<html>",
        b"Tipo Titulo;Data Vencimento;Data Base\nTesouro;01/01/2030;01/01/2026\n",
        _csv_payload(malformed_at=0).replace(b"11,25", b"NaN"),
        _csv_payload().replace(b"Tesouro Prefixado 0000", b""),
        _csv_payload().replace(b"Tesouro Prefixado 0000", b"\"Tesouro Prefixado 0000"),
    ],
)
def test_csv_validation_rejects_missing_scope_malformed_numeric_and_truncated_csv(payload: bytes) -> None:
    with pytest.raises(TreasuryCsvError):
        validate_treasury_csv(payload)


def test_csv_validation_rejects_duplicate_identity_and_invalid_content_length() -> None:
    row = _csv_payload()
    duplicate = row + row.splitlines(keepends=True)[1]
    with pytest.raises(TreasuryCsvError, match="duplicate"):
        validate_treasury_csv(duplicate)
    with pytest.raises(TreasuryCsvError, match="content length"):
        validate_treasury_csv(row, content_length="not-a-number")


def test_csv_validation_checks_finite_numeric_values_and_calendar_dates(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from decimal import Decimal

    from db import treasury_store

    with pytest.raises(TreasuryCsvError, match="financial value"):
        validate_treasury_csv(_csv_payload().replace(b"11,25", b"not-a-rate"))
    blank_values, _ = validate_treasury_csv(_csv_payload().replace(b"11,25", b""))
    assert blank_values[0]["values"]["Taxa Compra Manha"] == ""
    with pytest.raises(TreasuryCsvError, match="invalid date"):
        validate_treasury_csv(_csv_payload().replace(b"01/01/2030", b"32/13/2030"))
    monkeypatch.setattr(treasury_store, "Decimal", lambda _value: Decimal("NaN"))
    with pytest.raises(TreasuryCsvError, match="non-finite"):
        treasury_store._validate_optional_decimal("1,0")

    class InvalidDecimal:
        def __new__(cls, _value: str) -> None:
            raise treasury_store.InvalidOperation("invalid decimal")

    monkeypatch.setattr(treasury_store, "Decimal", InvalidDecimal)
    with pytest.raises(TreasuryCsvError, match="invalid financial value"):
        treasury_store._validate_optional_decimal("1,0")


def test_csv_validation_rejects_no_rows_and_expired_or_late_parse_deadlines() -> None:
    header_only = (";".join(FIELDS) + "\n").encode()
    with pytest.raises(TreasuryCsvError, match="no complete cohort"):
        validate_treasury_csv(header_only)
    with pytest.raises(TimeoutError, match="before CSV parsing"):
        validate_treasury_csv(_csv_payload(), deadline_at=time.monotonic() - 1)

    ticks = iter([90.0, 96.0])
    with pytest.raises(TimeoutError, match="while parsing"):
        validate_treasury_csv(
            _csv_payload(rows=1024), deadline_at=100.0, clock=lambda: next(ticks)
        )


def test_treasury_preparation_and_persistence_fail_closed_on_expired_deadline() -> None:
    parsed, digest = validate_treasury_csv(_csv_payload())
    with pytest.raises(TimeoutError, match="serializing"):
        prepare_treasury_rows(parsed, deadline_at=time.monotonic() - 1)
    with pytest.raises(TreasuryCsvError, match="no complete cohort"):
        persist_treasury_rows(
            [], extraction_id="empty", source_payload_sha256=digest,
            captured_at="2026-10-27T00:00:00+00:00", reference_min="2026-10-26",
            reference_max="2026-10-26", deadline_at=time.monotonic() + 30,
        )
    rows = prepare_treasury_rows(parsed)
    with pytest.raises(TimeoutError, match="before database persistence"):
        persist_treasury_rows(
            rows, extraction_id="expired", source_payload_sha256=digest,
            captured_at="2026-10-27T00:00:00+00:00", reference_min="2026-10-26",
            reference_max="2026-10-26", deadline_at=time.monotonic() - 1,
        )


def test_sqlite_atomic_commit_replay_and_conflicting_operation_id(sqlite_db: None) -> None:
    from db.store import get_meta, query_treasury_rows, treasury_cohort_commit

    _seed_old_sqlite_cohort()
    payload = _csv_payload(rows=3)
    rows, receipt = _prepared(payload, "stable-treasury-operation")
    committed = persist_treasury_rows(
        rows,
        extraction_id=receipt.extraction_id,
        source_payload_sha256=receipt.source_payload_sha256,
        captured_at=receipt.captured_at,
        reference_min=receipt.reference_min,
        reference_max=receipt.reference_max,
        deadline_at=time.monotonic() + 30,
    )
    replay = persist_treasury_rows(
        rows,
        extraction_id=receipt.extraction_id,
        source_payload_sha256=receipt.source_payload_sha256,
        captured_at="2026-10-27T00:00:00+00:00",
        reference_min=receipt.reference_min,
        reference_max=receipt.reference_max,
        deadline_at=time.monotonic() + 30,
    )

    assert committed == receipt
    assert replay == receipt
    assert len(query_treasury_rows()) == 3
    assert treasury_cohort_commit(receipt.extraction_id) == receipt
    assert get_meta("td_last_refresh") == receipt.captured_at

    with pytest.raises(ValueError, match="conflicts"):
        persist_treasury_rows(
            rows,
            extraction_id=receipt.extraction_id,
            source_payload_sha256="0" * 64,
            captured_at=receipt.captured_at,
            reference_min=receipt.reference_min,
            reference_max=receipt.reference_max,
            deadline_at=time.monotonic() + 30,
        )
    assert len(query_treasury_rows()) == 3


def test_sqlite_sql_failure_rolls_back_old_cohort_receipt_and_freshness(sqlite_db: None) -> None:
    from db.store import get_meta, query_treasury_rows, treasury_cohort_commit

    _seed_old_sqlite_cohort()
    payload = _csv_payload(rows=2)
    rows, receipt = _prepared(payload, "rollback-treasury-operation")
    duplicated = [rows[0], rows[0]]
    invalid_receipt = receipt._replace(observation_count=2)

    import sqlite3

    with pytest.raises(sqlite3.IntegrityError):
        store.commit_treasury_cohort(duplicated, invalid_receipt, deadline_at=time.monotonic() + 30)

    assert query_treasury_rows()[0]["Tipo Titulo"] == "Tesouro Prefixado old"
    assert treasury_cohort_commit(receipt.extraction_id) is None
    assert get_meta("td_last_refresh") == "old-freshness"


def test_sqlite_deadline_after_bulk_insert_rolls_back_before_commit(sqlite_db: None) -> None:
    from db.store import get_meta, query_treasury_rows, treasury_cohort_commit

    _seed_old_sqlite_cohort()
    rows, receipt = _prepared(_csv_payload(rows=2), "deadline-treasury-operation")
    ticks = iter([10.0, 10.0, 10.0, 10.0, 11.0])

    with pytest.raises(TimeoutError, match="deadline elapsed"):
        store.commit_treasury_cohort(rows, receipt, deadline_at=10.5, clock=lambda: next(ticks))

    assert query_treasury_rows()[0]["Tipo Titulo"] == "Tesouro Prefixado old"
    assert treasury_cohort_commit(receipt.extraction_id) is None
    assert get_meta("td_last_refresh") == "old-freshness"


def test_public_refresh_pipeline_downloads_validates_persists_and_logs_sqlite_cohort(sqlite_db: None) -> None:
    payload = _csv_payload(rows=4)
    response = SimpleNamespace(content=payload, headers={"content-length": str(len(payload))})

    with patch("datafetchers.tesouro_direto.get_provider_response_bounded", return_value=response):
        result = refresh_treasury_data()

    assert result == {"rows": 4}
    stored = store.query_treasury_rows()
    assert len(stored) == 4
    assert stored[0]["PU Compra Manha"] == "1.234,56"
    receipts = store._query("SELECT * FROM treasury_cohort_commits")
    assert len(receipts) == 1
    assert receipts[0]["source_payload_sha256"] == hashlib.sha256(payload).hexdigest()
    assert receipts[0]["observation_count"] == 4
    assert store.get_meta("td_last_refresh") == receipts[0]["captured_at"]
    extractions = store.latest_source_extractions(10)
    assert extractions[0]["status"] == "success"
    assert extractions[0]["persisted_count"] == 4
    assert extractions[0]["extraction_id"] == receipts[0]["extraction_id"]


def test_public_refresh_invalid_csv_keeps_previous_cohort_and_freshness(sqlite_db: None) -> None:
    _seed_old_sqlite_cohort()
    response = SimpleNamespace(content=_csv_payload(malformed_at=0), headers={})

    with (
        patch("datafetchers.tesouro_direto.get_provider_response_bounded", return_value=response),
        pytest.raises(TreasuryCsvError),
    ):
        refresh_treasury_data()

    assert store.query_treasury_rows()[0]["Tipo Titulo"] == "Tesouro Prefixado old"
    assert store.get_meta("td_last_refresh") == "old-freshness"
    assert store._query("SELECT * FROM treasury_cohort_commits") == []
    assert store.latest_source_extractions(10)[0]["status"] == "validation_error"


def test_public_refresh_postcommit_extraction_log_failure_keeps_committed_counts(sqlite_db: None) -> None:
    payload = _csv_payload(rows=2)
    response = SimpleNamespace(content=payload, headers={})

    with (
        patch("datafetchers.tesouro_direto.get_provider_response_bounded", return_value=response),
        patch("source_ingestion.finish_source_extraction", side_effect=RuntimeError("ledger unavailable")),
        pytest.raises(SourceOperationError) as raised,
    ):
        refresh_treasury_data()

    assert raised.value.stage == "logging"
    assert raised.value.committed_count == 2
    assert len(store.query_treasury_rows()) == 2
    assert len(store._query("SELECT * FROM treasury_cohort_commits")) == 1


PG_URL = os.environ.get("TEST_DATABASE_URL", "")
PG_TARGET = urlsplit(PG_URL)


def _assert_local_test_postgres() -> None:
    if PG_TARGET.hostname not in {"127.0.0.1", "localhost", "::1"} or not PG_TARGET.path.endswith("_test"):
        pytest.fail("TEST_DATABASE_URL must point to localhost and a *_test database")
    with psycopg.connect(PG_URL) as connection:
        row = connection.execute("SELECT current_database(), version()").fetchone()
    if row is None or row[0] != PG_TARGET.path.lstrip("/") or "PostgreSQL 17" not in row[1]:
        pytest.fail("Treasury integration test requires the selected PostgreSQL 17 *_test database")


def test_postgres_native_180k_cohort_atomicity_roundtrip_and_timeout(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    if not PG_URL:
        pytest.skip("requires local disposable PostgreSQL 17 TEST_DATABASE_URL")
    _assert_local_test_postgres()
    prior_database_url = store.DATABASE_URL
    created_extraction_ids: list[str] = []
    store.DATABASE_URL = PG_URL
    store._pg_local.conn = None
    try:
        store.init_db()
        with psycopg.connect(PG_URL) as connection:
            connection.execute("DELETE FROM treasury")
            connection.execute("DELETE FROM treasury_cohort_commits")
            connection.execute("DELETE FROM meta WHERE key = 'td_last_refresh'")
            connection.commit()
        payload = _csv_payload(rows=180_000)
        legacy_parse_started = time.perf_counter()
        legacy_parsed = list(csv.DictReader(io.StringIO(payload.decode("utf-8-sig"), newline=""), delimiter=";"))
        legacy_parse_seconds = time.perf_counter() - legacy_parse_started
        legacy_prepare_started = time.perf_counter()
        legacy_records = [
            {**item, "_bond_code": _bond_code(item["Tipo Titulo"], item["Data Vencimento"])}
            for item in legacy_parsed
        ]
        legacy_prepare_seconds = time.perf_counter() - legacy_prepare_started
        legacy_load_started = time.perf_counter()
        assert store.replace_treasury_rows(legacy_records) == 180_000
        legacy_load_seconds = time.perf_counter() - legacy_load_started
        started = time.perf_counter()
        parsed, digest = validate_treasury_csv(payload)
        parsed_at = time.perf_counter()
        rows = prepare_treasury_rows(parsed)
        prepared_at = time.perf_counter()
        dates = [item["base_date"] for item in parsed]
        receipt = TreasuryCohortReceipt(
            "postgres-180k-benchmark", digest, len(rows), min(dates), max(dates),
            datetime.now(timezone.utc).isoformat(),
        )
        stage_timings: dict[str, float] = {}
        result = store.commit_treasury_cohort(
            rows,
            receipt,
            deadline_at=time.monotonic() + 60,
            stage_timings=stage_timings,
        )
        committed_at = time.perf_counter()
        with psycopg.connect(PG_URL) as connection:
            row_count = connection.execute("SELECT count(*) FROM treasury").fetchone()[0]
            sample = connection.execute(
                "SELECT data_json FROM treasury ORDER BY bond_code, base_date LIMIT 1"
            ).fetchone()[0]
            stored_receipt = connection.execute(
                "SELECT observation_count, source_payload_sha256 FROM treasury_cohort_commits "
                "WHERE extraction_id = %s", (receipt.extraction_id,),
            ).fetchone()
        assert result == receipt
        assert row_count == 180_000
        assert json.loads(sample)["PU Compra Manha"] == "1.234,56"
        assert stored_receipt == (180_000, digest)

        old_hash = hashlib.sha256(b"old cohort").hexdigest()
        invalid_rows = [(rows[0][0], rows[0][1], rows[0][2]), (rows[0][0], rows[0][1], rows[0][2])]
        invalid_receipt = TreasuryCohortReceipt(
            "postgres-rollback", old_hash, 2, receipt.reference_min, receipt.reference_max,
            datetime.now(timezone.utc).isoformat(),
        )
        with pytest.raises(psycopg.errors.UniqueViolation):
            store.commit_treasury_cohort(invalid_rows, invalid_receipt, deadline_at=time.monotonic() + 10)
        with psycopg.connect(PG_URL) as connection:
            assert connection.execute("SELECT count(*) FROM treasury").fetchone()[0] == 180_000
            assert connection.execute(
                "SELECT count(*) FROM treasury_cohort_commits WHERE extraction_id = %s",
                (invalid_receipt.extraction_id,),
            ).fetchone()[0] == 0

        lock_payload_rows, lock_receipt = _prepared(_csv_payload(rows=2), "postgres-lock-timeout")
        with psycopg.connect(PG_URL) as blocker:
            blocker.execute("LOCK TABLE treasury IN ACCESS EXCLUSIVE MODE")
            with pytest.raises(psycopg.errors.LockNotAvailable):
                store.commit_treasury_cohort(
                    lock_payload_rows,
                    lock_receipt,
                    deadline_at=time.monotonic() + 4.0,
                )
        with psycopg.connect(PG_URL) as connection:
            assert connection.execute("SELECT count(*) FROM treasury").fetchone()[0] == 180_000
            assert connection.execute(
                "SELECT count(*) FROM treasury_cohort_commits WHERE extraction_id = %s",
                (lock_receipt.extraction_id,),
            ).fetchone()[0] == 0

        lost_ack_rows, lost_ack_receipt = _prepared(_csv_payload(rows=2), "postgres-commit-ack-lost")
        real_get_connection = store._get_pg_conn

        class CommitAcknowledgementLost:
            def __init__(self, connection):
                self.connection = connection
                self.first_commit = True

            def __getattr__(self, name):
                return getattr(self.connection, name)

            def commit(self):
                self.connection.commit()
                if self.first_commit:
                    self.first_commit = False
                    raise OSError("simulated commit acknowledgement loss")

        monkeypatch.setattr(store, "_get_pg_conn", lambda: CommitAcknowledgementLost(real_get_connection()))
        recovered = store.commit_treasury_cohort(
            lost_ack_rows,
            lost_ack_receipt,
            deadline_at=time.monotonic() + 10,
        )
        assert recovered == lost_ack_receipt
        with psycopg.connect(PG_URL) as connection:
            assert connection.execute("SELECT count(*) FROM treasury").fetchone()[0] == 2
            assert connection.execute(
                "SELECT source_payload_sha256 FROM treasury_cohort_commits WHERE extraction_id = %s",
                (lost_ack_receipt.extraction_id,),
            ).fetchone()[0] == lost_ack_receipt.source_payload_sha256
        monkeypatch.setattr(store, "_get_pg_conn", real_get_connection)

        pipeline_payload = _csv_payload(rows=3)
        response = SimpleNamespace(content=pipeline_payload, headers={})
        with patch("datafetchers.tesouro_direto.get_provider_response_bounded", return_value=response):
            assert refresh_treasury_data() == {"rows": 3}
        with psycopg.connect(PG_URL) as connection:
            pipeline_receipt = connection.execute(
                "SELECT extraction_id, source_payload_sha256, observation_count "
                "FROM treasury_cohort_commits WHERE source_payload_sha256 = %s",
                (hashlib.sha256(pipeline_payload).hexdigest(),),
            ).fetchone()
            assert connection.execute("SELECT count(*) FROM treasury").fetchone()[0] == 3
            assert pipeline_receipt is not None
            created_extraction_ids.append(pipeline_receipt[0])
            extraction = connection.execute(
                "SELECT status, network_called, persisted_count FROM source_extractions "
                "WHERE extraction_id = %s",
                (pipeline_receipt[0],),
            ).fetchone()
        assert pipeline_receipt[2] == 3
        assert extraction == ("success", 1, 3)

        print(
            "TREASURY_NATIVE_PG17_BENCHMARK "
            f"rows={row_count} bytes={len(payload)} "
            f"legacy_parse={legacy_parse_seconds:.3f}s legacy_prepare={legacy_prepare_seconds:.3f}s "
            f"legacy_executemany_replace_commit={legacy_load_seconds:.3f}s "
            f"bounded_validate_parse={parsed_at-started:.3f}s bounded_serialize={prepared_at-parsed_at:.3f}s "
            f"copy_stage={stage_timings['copy_stage_seconds']:.3f}s "
            f"atomic_replace={stage_timings['atomic_replace_seconds']:.3f}s "
            f"receipt_freshness={stage_timings['receipt_and_freshness_seconds']:.3f}s "
            f"commit={stage_timings['database_commit_seconds']:.3f}s "
            f"bounded_total={committed_at-started:.3f}s"
        )
    finally:
        with psycopg.connect(PG_URL) as connection:
            connection.execute("DELETE FROM treasury")
            connection.execute("DELETE FROM treasury_cohort_commits")
            connection.execute("DELETE FROM meta WHERE key = 'td_last_refresh'")
            if created_extraction_ids:
                connection.execute(
                    "DELETE FROM source_extractions WHERE extraction_id = ANY(%s)",
                    (created_extraction_ids,),
                )
            connection.commit()
        store._pg_local.conn = None
        store.DATABASE_URL = prior_database_url
