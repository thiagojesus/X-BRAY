import hashlib
import json
import sqlite3
from contextlib import closing
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

import pytest

from datafetchers.focus import FOCUS_12M_KEY
from db import store
from tests.real_rate_support import isolated_database

pytestmark = pytest.mark.usefixtures(isolated_database.__name__)


@dataclass(frozen=True, slots=True)
class _VintageSeed:
    provider: str
    series_key: str
    reference_period: str
    record: dict[str, str | int | float]
    available_at: datetime


def _insert_vintage(seed: _VintageSeed) -> None:
    dimensions_json = "{}"
    dimensions_hash = hashlib.sha256(dimensions_json.encode("utf-8")).hexdigest()
    raw_value_json = json.dumps(
        seed.record,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    )
    payload_hash = hashlib.sha256(raw_value_json.encode("utf-8")).hexdigest()
    observed_at = seed.available_at.isoformat()
    store.execute_change(
        "INSERT INTO observation_vintages "
        "(provider, series_key, reference_period, dimensions_json, dimensions_hash, raw_value_json, "
        "source_published_at, first_seen_at, available_at, payload_hash, revision_id) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (
            seed.provider,
            seed.series_key,
            seed.reference_period,
            dimensions_json,
            dimensions_hash,
            raw_value_json,
            None,
            observed_at,
            observed_at,
            payload_hash,
            payload_hash[:16],
        ),
    )


def _require_source_loader() -> None:
    source_path = Path(__file__).parents[1] / "macro_score" / "sources.py"
    if not source_path.is_file():
        pytest.fail("[RED XBR-601] point-in-time source adapters have not been implemented")


def test_red_xbr_601_loads_only_score_vintages_known_by_the_cutoff() -> None:
    _require_source_loader()
    from macro_score.sources import load_score_vintages

    first_seen = datetime(2026, 9, 10, tzinfo=timezone.utc)
    revised_at = datetime(2026, 9, 20, tzinfo=timezone.utc)
    _insert_vintage(_VintageSeed(
        "bcb_sgs", "saldo_primario_12m_pib", "2026-08-31", {"valor": -0.15}, first_seen
    ))
    _insert_vintage(_VintageSeed(
        "bcb_sgs", "resultado_primario", "2026-08-31", {"valor": 250.0}, first_seen
    ))
    _insert_vintage(_VintageSeed(
        "bcb_sgs", "saldo_primario_12m_pib", "2026-08-31", {"valor": -0.25}, revised_at
    ))
    _insert_vintage(_VintageSeed(
        "focus",
        FOCUS_12M_KEY,
        "2026-09-18",
        {
            "Indicador": "IPCA",
            "Data": "2026-09-18",
            "Mediana": 4.7,
            "Suavizada": "S",
            "baseCalculo": 0,
        },
        revised_at,
    ))
    _insert_vintage(_VintageSeed(
        "focus",
        "IPCA",
        "2026-09-18",
        {"Indicador": "IPCA", "Data": "2026-09-18", "Mediana": 99.0},
        revised_at,
    ))

    before_revision = load_score_vintages(
        as_of=datetime(2026, 9, 15, tzinfo=timezone.utc)
    )
    after_revision = load_score_vintages(
        as_of=datetime(2026, 9, 21, tzinfo=timezone.utc)
    )
    primary_before = tuple(item for item in before_revision if item.series_key == "saldo_primario_12m_pib")
    primary_after = tuple(item for item in after_revision if item.series_key == "saldo_primario_12m_pib")
    focus_before = tuple(item for item in before_revision if item.provider == "focus")
    focus_after = tuple(item for item in after_revision if item.provider == "focus")

    assert len(primary_before) == 1
    assert primary_before[0].value == 0.15
    assert primary_before[0].unit == "pct_gdp"
    assert primary_before[0].available_at == first_seen
    assert primary_before[0].source_published_at is None
    assert len(primary_after) == 2
    assert {item.value for item in primary_after} == {0.15, 0.25}
    assert focus_before == ()
    assert len(focus_after) == 1
    assert focus_after[0].series_key == FOCUS_12M_KEY
    assert focus_after[0].value == 4.7
    assert all(item.series_key != "resultado_primario" for item in after_revision)


def test_score_vintage_loader_reads_optional_sqlite_source_read_only(tmp_path: Path) -> None:
    from macro_score.sources import load_score_vintages

    target = tmp_path / "focus-source.db"
    with closing(sqlite3.connect(target)) as connection:
        connection.execute("""CREATE TABLE observation_vintages (
            id INTEGER PRIMARY KEY, provider TEXT, series_key TEXT, reference_period TEXT,
            dimensions_json TEXT, dimensions_hash TEXT, raw_value_json TEXT, source_published_at TEXT,
            first_seen_at TEXT, available_at TEXT, payload_hash TEXT, revision_id TEXT)""")
        raw = json.dumps({"Indicador": "IPCA", "Data": "2026-09-18", "Mediana": 4.7,
                          "Suavizada": "S", "baseCalculo": 0}, sort_keys=True)
        connection.execute(
            "INSERT INTO observation_vintages VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
            (1, "focus", FOCUS_12M_KEY, "2026-09-18", "{}", "dimensions", raw, None,
             "2026-09-20T00:00:00+00:00", "2026-09-20T00:00:00+00:00", "payload", "revision"),
        )
        connection.commit()
    rows = load_score_vintages(
        as_of=datetime(2026, 9, 21, tzinfo=timezone.utc), database=target,
    )
    assert len(rows) == 1 and rows[0].value == 4.7
    assert rows[0].available_at == datetime(2026, 9, 20, tzinfo=timezone.utc)


def test_retrospective_di_loader_enforces_real_fetch_cutoff_and_source_basis() -> None:
    from macro_score.sources import load_retrospective_di_observations

    store.execute_change(
        "INSERT INTO retrospective_di_observations "
        "(trade_date,symbol,maturity,rate,source_hash,fetched_at,revision,source_basis,source_name,source_member) "
        "VALUES (?,?,?,?,?,?,?,?,?,?)",
        ("2024-01-30", "DI1F25", "2025-01-02", 12.0, "a" * 64,
         "2026-09-10T00:00:00+00:00", "rev-a", "retrospective_reference", "file.zip", "file.zip!a.xml"),
    )
    store.execute_change(
        "INSERT INTO retrospective_di_observations "
        "(trade_date,symbol,maturity,rate,source_hash,fetched_at,revision,source_basis,source_name,source_member) "
        "VALUES (?,?,?,?,?,?,?,?,?,?)",
        ("2024-01-30", "DI1G25", "2025-02-03", 13.0, "b" * 64,
         "2026-10-01T00:00:00+00:00", "rev-b", "current", "file.zip", "file.zip!b.xml"),
    )
    store.execute_change(
        "INSERT INTO retrospective_di_observations "
        "(trade_date,symbol,maturity,rate,source_hash,fetched_at,revision,source_basis,source_name,source_member) "
        "VALUES (?,?,?,?,?,?,?,?,?,?)",
        ("2026-10-21", "DI1H27", "2027-03-01", 14.0, "c" * 64,
         "2026-10-01T00:00:00+00:00", "rev-c", "retrospective_reference", "future.zip", "future.zip!c.xml"),
    )
    store.execute_change(
        "INSERT INTO retrospective_di_observations "
        "(trade_date,symbol,maturity,rate,source_hash,fetched_at,revision,source_basis,source_name,source_member) "
        "VALUES (?,?,?,?,?,?,?,?,?,?)",
        ("2024-01-30", "DI1V25", "2025-10-01", 14.0, "d" * 64,
         "2026-10-21T00:00:00+00:00", "rev-d", "retrospective_reference", "later.zip", "later.zip!d.xml"),
    )
    for symbol, source_hash, security_ids, stored_id in (
        ("DI1G25", "e" * 64, ("200002",), "wrong-200002"),
        ("DI1H25", "f" * 64, ("200003", "200004"), "200003"),
        ("DI1I25", "g" * 64, ("200005",), "200005"),
    ):
        store.execute_change(
            "INSERT INTO retrospective_di_observations "
            "(trade_date,symbol,maturity,rate,source_hash,fetched_at,revision,source_basis,source_name,source_member,security_id) "
            "VALUES (?,?,?,?,?,?,?,?,?,?,?)",
            ("2024-01-30", symbol, "2025-02-03", 13.0, source_hash,
             "2026-09-11T00:00:00+00:00", f"rev-{symbol}", "retrospective_reference",
             "file.zip", f"file.zip!{symbol}.xml", stored_id),
        )
        for index, security_id in enumerate(security_ids):
            store.execute_change(
                "INSERT INTO retrospective_di_price_identities "
                "(trade_date,symbol,source_hash,source_member,security_id,captured_at,source_basis) "
                "VALUES (?,?,?,?,?,?,?)",
                ("2024-01-30", symbol, source_hash, f"cache-{index}.xml", security_id,
                 "2026-09-11T00:00:00+00:00", "b3_price_identity_v1"),
            )
    known = load_retrospective_di_observations(
        as_of=datetime(2026, 10, 20, tzinfo=timezone.utc)
    )
    by_symbol = {item.symbol: item for item in known}
    assert set(by_symbol) == {"DI1F25", "DI1G25", "DI1H25", "DI1I25"}
    assert by_symbol["DI1F25"].security_id is None
    assert by_symbol["DI1G25"].security_id is None
    assert by_symbol["DI1G25"].security_identity_conflict is True
    assert by_symbol["DI1H25"].security_id is None
    assert by_symbol["DI1H25"].security_identity_conflict is True
    assert by_symbol["DI1I25"].security_id == "200005"
    assert by_symbol["DI1I25"].security_identity_conflict is False
    assert by_symbol["DI1F25"].fetched_at == datetime(2026, 9, 10, tzinfo=timezone.utc)


def test_retrospective_loader_reads_configured_external_ledger_read_only(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    from macro_score.sources import load_retrospective_di_observations

    target = tmp_path / "isolated-retro.db"
    with closing(sqlite3.connect(target)) as connection:
        connection.execute("""CREATE TABLE retrospective_di_observations (
            trade_date TEXT,symbol TEXT,maturity TEXT,rate REAL,source_hash TEXT,
            source_name TEXT,source_member TEXT,fetched_at TEXT,source_basis TEXT)""")
        connection.execute("""CREATE TABLE retrospective_di_price_identities (
            trade_date TEXT,symbol TEXT,source_hash TEXT,source_member TEXT,security_id TEXT,
            captured_at TEXT,source_basis TEXT)""")
        connection.execute(
            "INSERT INTO retrospective_di_observations VALUES (?,?,?,?,?,?,?,?,?)",
            ("2025-01-30", "DI1F26", "2026-01-02", 12.0, "e" * 64,
             "source.zip", "source.zip!bulletin.xml", "2026-10-20T00:00:00+00:00",
             "retrospective_reference"),
        )
        connection.execute(
            "INSERT INTO retrospective_di_price_identities VALUES (?,?,?,?,?,?,?)",
            ("2025-01-30", "DI1F26", "e" * 64, "cache.zip!nested.xml", "200001",
             "2026-10-20T00:00:00+00:00", "b3_price_identity_v1"),
        )
        connection.commit()
    monkeypatch.setenv("XBRAY_RETROSPECTIVE_DI_SQLITE", str(target))
    result = load_retrospective_di_observations(
        as_of=datetime(2026, 10, 21, tzinfo=timezone.utc)
    )
    assert [item.symbol for item in result] == ["DI1F26"]
    assert result[0].security_id == "200001"
    with closing(sqlite3.connect(target)) as connection:
        assert connection.execute("SELECT COUNT(*) FROM retrospective_di_observations").fetchone()[0] == 1


def test_price_identity_conflicts_fail_closed_and_missing_mapping_is_unchanged(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    from macro_score.sources import load_retrospective_di_observations

    target = tmp_path / "identity-conflicts.db"
    with closing(sqlite3.connect(target)) as connection:
        connection.execute("""CREATE TABLE retrospective_di_observations (
            trade_date TEXT,symbol TEXT,maturity TEXT,rate REAL,source_hash TEXT,
            source_name TEXT,source_member TEXT,fetched_at TEXT,source_basis TEXT,
            maturity_basis TEXT,security_id TEXT)""")
        connection.execute("""CREATE TABLE retrospective_di_price_identities (
            trade_date TEXT,symbol TEXT,source_hash TEXT,source_member TEXT,security_id TEXT,
            captured_at TEXT,source_basis TEXT)""")
        fetched = "2026-10-20T00:00:00+00:00"
        for symbol, source_hash, stored_id in (
            ("DI1A25", "a" * 64, "stored-a"),
            ("DI1B25", "b" * 64, "stored-b"),
            ("DI1C25", "c" * 64, "stored-c"),
            ("DI1D25", "d" * 64, None),
        ):
            connection.execute(
                "INSERT INTO retrospective_di_observations VALUES (?,?,?,?,?,?,?,?,?,?,?)",
                ("2025-01-30", symbol, "2026-01-02", 12.0, source_hash,
                 "source.zip", f"source.zip!{symbol}.xml", fetched,
                 "retrospective_reference", "b3_instrument_snapshot_v1", stored_id),
            )
        identity_rows = (
            ("DI1A25", "a" * 64, "identity-a1"),
            ("DI1A25", "a" * 64, "identity-a2"),
            ("DI1B25", "b" * 64, "different-b"),
            ("DI1D25", "d" * 64, None),
        )
        for index, (symbol, source_hash, security_id) in enumerate(identity_rows):
            connection.execute(
                "INSERT INTO retrospective_di_price_identities VALUES (?,?,?,?,?,?,?)",
                ("2025-01-30", symbol, source_hash, f"cache-{index}.xml", security_id,
                 fetched, "b3_price_identity_v1"),
            )
        connection.commit()

    monkeypatch.setenv("XBRAY_RETROSPECTIVE_DI_SQLITE", str(target))
    rows = load_retrospective_di_observations(
        as_of=datetime(2026, 10, 21, tzinfo=timezone.utc)
    )
    by_symbol = {item.symbol: item for item in rows}
    assert by_symbol["DI1A25"].security_id is None
    assert by_symbol["DI1A25"].security_identity_conflict is True
    assert by_symbol["DI1B25"].security_id is None
    assert by_symbol["DI1B25"].security_identity_conflict is True
    assert by_symbol["DI1C25"].security_id == "stored-c"
    assert by_symbol["DI1C25"].security_identity_conflict is False
    assert by_symbol["DI1D25"].security_id is None
    assert by_symbol["DI1D25"].security_identity_conflict is True


def test_instrument_snapshot_loader_uses_real_availability_cutoff(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    from macro_score.sources import load_di_instrument_snapshots

    target = tmp_path / "catalog-ledger.db"
    with closing(sqlite3.connect(target)) as connection:
        connection.execute("""CREATE TABLE b3_di_instrument_snapshots (
            snapshot_date TEXT,report_date TEXT,symbol TEXT,security_id TEXT,fin_instrm_id TEXT,
            maturity TEXT,trading_start TEXT,trading_end TEXT,source_hash TEXT,source_name TEXT,
            message_definition TEXT,message_id TEXT,captured_at TEXT,available_at TEXT,source_basis TEXT)""")
        connection.execute(
            "INSERT INTO b3_di_instrument_snapshots VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            ("2026-10-19", "2026-10-19", "DI1F27", "BRISIN", "200001", "2027-01-04",
             "2020-01-01", "2026-12-30", "a" * 64, "catalog.xml", "bvmf.100.02", "msg-1",
             "2026-10-20T00:00:00+00:00", "2026-10-20T00:00:00+00:00", "b3_instrument_snapshot_v1"),
        )
        connection.commit()
    monkeypatch.setenv("XBRAY_RETROSPECTIVE_DI_SQLITE", str(target))
    before = load_di_instrument_snapshots(as_of=datetime(2026, 10, 19, tzinfo=timezone.utc))
    after = load_di_instrument_snapshots(as_of=datetime(2026, 10, 21, tzinfo=timezone.utc))
    assert before == ()
    assert len(after) == 1 and after[0].maturity.isoformat() == "2027-01-04"


def test_macro_score_source_loader_includes_cutoff_eligible_di_instrument_registry() -> None:
    from macro_score.sources import load_macro_score_sources

    available = "2026-10-20T00:00:00+00:00"
    for symbol, availability in (("DI1F27", available), ("DI1G27", "2026-10-22T00:00:00+00:00")):
        store.execute_change(
            "INSERT INTO b3_di_instrument_snapshots "
            "(snapshot_date,report_date,symbol,security_id,fin_instrm_id,maturity,trading_start,trading_end,"
            "source_hash,source_name,message_definition,message_id,captured_at,available_at,source_basis) "
            "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            ("2026-10-19", "2026-10-19", symbol, f"isin-{symbol}", f"id-{symbol}", "2027-01-04",
             "2020-01-01", "2026-12-30", f"hash-{symbol}", "catalog.xml", "bvmf.100.02",
             f"message-{symbol}", available, availability, "b3_instrument_snapshot_v1"),
        )

    sources = load_macro_score_sources(as_of=datetime(2026, 10, 21, tzinfo=timezone.utc))

    assert [item.symbol for item in sources.di_instrument_snapshots] == ["DI1F27"]
    assert sources.di_instrument_snapshots[0].available_at == datetime(2026, 10, 20, tzinfo=timezone.utc)
