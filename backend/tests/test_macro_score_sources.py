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
    known = load_retrospective_di_observations(
        as_of=datetime(2026, 10, 20, tzinfo=timezone.utc)
    )
    assert len(known) == 1
    assert known[0].symbol == "DI1F25"
    assert known[0].fetched_at == datetime(2026, 9, 10, tzinfo=timezone.utc)


def test_retrospective_loader_reads_configured_external_ledger_read_only(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    from macro_score.sources import load_retrospective_di_observations

    target = tmp_path / "isolated-retro.db"
    with closing(sqlite3.connect(target)) as connection:
        connection.execute("""CREATE TABLE retrospective_di_observations (
            trade_date TEXT,symbol TEXT,maturity TEXT,rate REAL,source_hash TEXT,
            source_name TEXT,source_member TEXT,fetched_at TEXT,source_basis TEXT)""")
        connection.execute(
            "INSERT INTO retrospective_di_observations VALUES (?,?,?,?,?,?,?,?,?)",
            ("2025-01-30", "DI1F26", "2026-01-02", 12.0, "e" * 64,
             "source.zip", "source.zip!bulletin.xml", "2026-10-20T00:00:00+00:00",
             "retrospective_reference"),
        )
        connection.commit()
    monkeypatch.setenv("XBRAY_RETROSPECTIVE_DI_SQLITE", str(target))
    result = load_retrospective_di_observations(
        as_of=datetime(2026, 10, 21, tzinfo=timezone.utc)
    )
    assert [item.symbol for item in result] == ["DI1F26"]
    with closing(sqlite3.connect(target)) as connection:
        assert connection.execute("SELECT COUNT(*) FROM retrospective_di_observations").fetchone()[0] == 1
