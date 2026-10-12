import json
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import patch

import pytest
from fastapi.testclient import TestClient

from derived.models import DerivedInput, DerivedResult, SnapshotOrigin
from datafetchers.focus import FOCUS_12M_KEY
from db import store
from derived.repository import insert_snapshot, refresh_derived_snapshots
from main import app


@pytest.fixture(autouse=True)
def isolated_database(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    from db import store

    if store._conn is not None:
        store._conn.close()
    store._conn = None
    monkeypatch.setattr(store, "DB_PATH", tmp_path / "derived-route.db")
    monkeypatch.setattr(store, "DATABASE_URL", "")
    store.init_db()
    yield
    if store._conn is not None:
        store._conn.close()
    store._conn = None


def test_get_derivados_is_cache_only_and_serializes_provenance() -> None:
    as_of = datetime(2026, 9, 27, 12, tzinfo=timezone.utc)
    result = DerivedResult(
        key="sample",
        value=5.0,
        unit="ratio",
        as_of=as_of,
        methodology_version="1.0.0",
        formula="series_a / 2",
        inputs=(DerivedInput(
            key="series_a",
            value=10.0,
            unit="pct",
            reference_period="2026-08-01",
            available_at=as_of,
            source="fixture",
        ),),
        reason=None,
    )
    insert_snapshot(result, calculation_cycle_id="cycle-a", origin=SnapshotOrigin.TEST_FIXTURE)

    with patch("datafetchers.ibge_sidra.httpx.get", side_effect=AssertionError("network access")):
        response = TestClient(app).get("/api/derivados")

    assert response.status_code == 200
    payload = response.json()
    assert payload["status"] == "ok"
    assert payload["data"]["sample"]["formula"] == "series_a / 2"
    assert payload["data"]["sample"]["methodology_version"] == "1.0.0"
    assert payload["data"]["sample"]["inputs"]["series_a"]["available_at"] == as_of.isoformat()


def test_empty_snapshot_cache_returns_empty_map_without_network() -> None:
    with patch("datafetchers.ibge_sidra.httpx.get", side_effect=AssertionError("network access")):
        response = TestClient(app).get("/api/derivados")

    assert response.status_code == 200
    assert response.json() == {"status": "unavailable", "data": {}}


def test_real_rate_route_is_cache_only_and_matches_frozen_contract() -> None:
    as_of = datetime(2026, 9, 27, 12, tzinfo=timezone.utc)
    available_at = datetime(2026, 9, 27, 10, tzinfo=timezone.utc)
    reference_date = date(2026, 9, 25)
    with patch("db.store._utc_now", return_value=available_at.isoformat()):
        store.upsert_focus(FOCUS_12M_KEY, [{
            "Indicador": "IPCA",
            "Data": reference_date.isoformat(),
            "Mediana": 4.66,
            "Suavizada": "S",
            "baseCalculo": 0,
        }])
        store.upsert_b3_di([{
            "trade_date": reference_date.isoformat(),
            "symbol": "DI1V27",
            "maturity": (reference_date + timedelta(days=365)).isoformat(),
            "rate": 11.82,
        }])
    assert refresh_derived_snapshots(as_of, "route-cycle") == 1
    contract_path = Path(__file__).parent / "fixtures" / "contracts" / "juro_real_ex_ante_1y.json"
    expected = json.loads(contract_path.read_text())

    with (
        patch(
            "datafetchers.focus.get_provider_response",
            side_effect=AssertionError("network access"),
        ),
        patch(
            "datafetchers.b3_di.get_provider_response",
            side_effect=AssertionError("network access"),
        ),
    ):
        response = TestClient(app).get("/api/derivados/juro-real-ex-ante-1y")

    assert response.status_code == 200
    assert response.json() == expected
