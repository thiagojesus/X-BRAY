from datetime import datetime, timedelta, timezone
from unittest.mock import patch

import pytest
from fastapi.testclient import TestClient

from db import store
from derived.models import DerivedInput, DerivedResult, SnapshotOrigin, UnavailableReason
from derived.repository import insert_snapshot
from main import app
from tests.real_rate_support import isolated_database

pytestmark = pytest.mark.usefixtures(isolated_database.__name__)

CUTOFF = datetime(2026, 10, 14, 14, tzinfo=timezone.utc)
_INDICATOR_KEYS = (
    "juro_real_ex_ante_1y",
    "cobertura_externa",
    "variacao_saldo_primario_12m",
    "spread_credito_pf_selic",
    "distancia_focus_meta",
    "inclinacao_di_5y_1y",
)


def _insert_snapshot(
    *,
    key: str,
    reference_date: str | None,
    value: float | None,
    as_of: datetime,
    reason: UnavailableReason | None = None,
    origin: SnapshotOrigin = SnapshotOrigin.PRODUCTION_REFRESH,
    created_at: datetime | None = None,
) -> None:
    inputs = () if value is None else (
        DerivedInput(
            key="source_value",
            value=value,
            unit="fixture_unit",
            reference_period=reference_date or "2026-10-14",
            available_at=as_of - timedelta(minutes=1),
            source="fixture",
        ),
    )
    result = DerivedResult(
        key=key,
        value=value,
        unit="fixture_unit",
        as_of=as_of,
        methodology_version="1.0.0",
        formula="fixture",
        inputs=inputs,
        reason=reason if value is None else None,
        reference_date=reference_date,
    )
    insert_snapshot(
        result,
        calculation_cycle_id=f"{key}-{as_of.isoformat()}-{reference_date}",
        origin=origin,
        created_at=created_at or as_of + timedelta(seconds=1),
    )


def test_derived_history_returns_bounded_persisted_points_and_preserves_gaps() -> None:
    # Given
    _insert_snapshot(
        key="juro_real_ex_ante_1y",
        reference_date="2024-10-31",
        value=2.0,
        as_of=CUTOFF - timedelta(days=1),
    )
    _insert_snapshot(
        key="juro_real_ex_ante_1y",
        reference_date="2024-11-03",
        value=4.0,
        as_of=CUTOFF - timedelta(hours=3),
    )
    _insert_snapshot(
        key="juro_real_ex_ante_1y",
        reference_date="2024-11-28",
        value=6.0,
        as_of=CUTOFF - timedelta(hours=2),
    )
    _insert_snapshot(
        key="juro_real_ex_ante_1y",
        reference_date="2024-11-28",
        value=7.0,
        as_of=CUTOFF - timedelta(hours=1),
    )
    _insert_snapshot(
        key="juro_real_ex_ante_1y",
        reference_date="2025-01-15",
        value=None,
        as_of=CUTOFF - timedelta(hours=1),
        reason=UnavailableReason.DI_CURVE_MISSING,
    )
    _insert_snapshot(
        key="juro_real_ex_ante_1y",
        reference_date="2025-02-10",
        value=8.0,
        as_of=CUTOFF + timedelta(seconds=1),
        created_at=CUTOFF + timedelta(seconds=2),
    )
    _insert_snapshot(
        key="juro_real_ex_ante_1y",
        reference_date="2025-03-10",
        value=9.0,
        as_of=CUTOFF - timedelta(hours=1),
        origin=SnapshotOrigin.TEST_FIXTURE,
    )
    _insert_snapshot(
        key="juro_real_ex_ante_1y",
        reference_date="2026-10-15",
        value=10.0,
        as_of=CUTOFF - timedelta(hours=1),
    )
    _insert_snapshot(
        key="inclinacao_di_5y_1y",
        reference_date="2024-11-20",
        value=-0.4,
        as_of=CUTOFF - timedelta(hours=1),
    )
    before_count = len(store.query_rows("SELECT id FROM derived_snapshots"))
    clock = patch("routes.derivados.datetime", wraps=datetime, create=True)

    # When
    with clock as frozen_clock:
        frozen_clock.now.return_value = CUTOFF
        response = TestClient(app).get("/api/derivados/history")

    # Then
    assert response.status_code == 200
    payload = response.json()
    assert payload["window"] == {
        "start_month": "2024-11",
        "end_month": "2026-10",
        "cutoff": CUTOFF.isoformat(),
    }
    assert set(payload["data"]) == set(_INDICATOR_KEYS)
    real_rate = payload["data"]["juro_real_ex_ante_1y"]
    assert [(item["month"], item["snapshot"]["reference_date"]) for item in real_rate] == [
        ("2024-11", "2024-11-28"),
        ("2025-01", "2025-01-15"),
    ]
    assert all(item["snapshot"]["reference_date"] <= "2026-10-14" for item in real_rate)
    assert real_rate[0]["snapshot"]["value"] == 7.0
    assert real_rate[0]["snapshot"]["as_of"] == (CUTOFF - timedelta(hours=1)).isoformat()
    assert real_rate[1]["snapshot"]["value"] is None
    assert real_rate[1]["snapshot"]["reason"] == "di_curve_missing"
    assert payload["data"]["inclinacao_di_5y_1y"][0]["snapshot"]["value"] == -0.4
    assert all(not payload["data"][key] for key in set(_INDICATOR_KEYS) - {
        "juro_real_ex_ante_1y", "inclinacao_di_5y_1y",
    })
    assert len(store.query_rows("SELECT id FROM derived_snapshots")) == before_count


def test_empty_derived_history_returns_all_series_without_network() -> None:
    # Given
    with (
        patch("datafetchers.b3_di.get_provider_response", side_effect=AssertionError("network access")),
        patch("datafetchers.focus.get_provider_response", side_effect=AssertionError("network access")),
    ):
        # When
        response = TestClient(app).get("/api/derivados/history")

    # Then
    assert response.status_code == 200
    assert response.json()["status"] == "unavailable"
    assert response.json()["data"] == {key: [] for key in _INDICATOR_KEYS}
