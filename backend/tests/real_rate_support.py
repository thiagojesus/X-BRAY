from collections.abc import Iterator
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import patch

import pytest

from db import store
from derived.models import DerivedInput, DerivedResult
from derived.repository import query_latest_snapshots, refresh_derived_snapshots

REAL_RATE_KEY = "juro_real_ex_ante_1y"
FOCUS_12M_KEY = "IPCA_12M_S_BASE0"
AS_OF = datetime(2026, 9, 27, 12, tzinfo=timezone.utc)
AVAILABLE_AT = datetime(2026, 9, 27, 10, tzinfo=timezone.utc)


@pytest.fixture(autouse=True)
def isolated_database(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> Iterator[None]:
    if store._conn is not None:
        store._conn.close()
    store._conn = None
    monkeypatch.setattr(store, "DB_PATH", tmp_path / "real-rate.db")
    monkeypatch.setattr(store, "DATABASE_URL", "")
    store.init_db()
    yield
    if store._conn is not None:
        store._conn.close()
    store._conn = None


def _seed_focus(
    *,
    reference_date: str = "2026-09-25",
    median: float = 4.66,
    available_at: datetime = AVAILABLE_AT,
    series_key: str = FOCUS_12M_KEY,
) -> None:
    record = {
        "Indicador": "IPCA",
        "Data": reference_date,
        "Mediana": median,
        "Suavizada": "S",
        "baseCalculo": 0,
    }
    with patch("db.store._utc_now", return_value=available_at.isoformat()):
        store.upsert_focus(series_key, [record])


def _seed_di(
    *,
    trade_date: str = "2026-09-25",
    maturity: str | None = None,
    symbol: str = "DI1V27",
    rate: float = 11.82,
    available_at: datetime = AVAILABLE_AT,
) -> None:
    reference = date.fromisoformat(trade_date)
    resolved_maturity = maturity or (reference + timedelta(days=365)).isoformat()
    with patch("db.store._utc_now", return_value=available_at.isoformat()):
        store.upsert_b3_di([
            {
                "trade_date": trade_date,
                "symbol": symbol,
                "maturity": resolved_maturity,
                "rate": rate,
            }
        ])


def _refresh_result(
    *, as_of: datetime = AS_OF, cycle: str = "cycle-current"
) -> DerivedResult:
    inserted = refresh_derived_snapshots(as_of, cycle)
    assert inserted == 1
    return query_latest_snapshots()[REAL_RATE_KEY].result


def _history_result(
    *,
    reference_date: date,
    value: float,
    as_of: datetime,
) -> DerivedResult:
    available_at = as_of - timedelta(hours=1)
    return DerivedResult(
        key=REAL_RATE_KEY,
        value=value,
        unit="pct_annual",
        as_of=as_of,
        methodology_version="1.0.0",
        formula="((1 + DI/100) / (1 + IPCA/100) - 1) * 100",
        inputs=(
            DerivedInput(
                key="nominal_rate",
                value=value + 4.0,
                unit="pct_annual",
                reference_period=reference_date.isoformat(),
                available_at=available_at,
                source="B3 DI",
            ),
            DerivedInput(
                key="expected_inflation",
                value=4.0,
                unit="pct_annual",
                reference_period=reference_date.isoformat(),
                available_at=available_at,
                source="Focus IPCA 12m",
            ),
        ),
        reason=None,
        reference_date=reference_date.isoformat(),
        historical_percentile=None,
        classification=None,
        classification_reason="insufficient_history",
    )
