from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import patch

import pytest
from fastapi.testclient import TestClient

from derived.models import DerivedInput, DerivedResult, SnapshotOrigin
from derived.repository import insert_snapshot
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
