from datetime import datetime, timedelta, timezone
from unittest.mock import patch

import pytest
from fastapi.testclient import TestClient

from derived.models import SnapshotOrigin
from macro_score.repository import (
    insert_macro_score_snapshot,
    query_latest_macro_score,
)
from main import app
from tests.macro_score_support import build_macro_score_result
from tests.real_rate_support import isolated_database

pytestmark = pytest.mark.usefixtures(isolated_database.__name__)


def test_red_xbr_601_latest_macro_score_is_cache_only_and_typed_when_empty() -> None:
    with (
        patch("provider_http.get_provider_response", side_effect=AssertionError("provider call")),
        patch("macro_score.sources.load_macro_score_sources", side_effect=AssertionError("input load")),
        patch("macro_score.calculator.aggregate_macro_score", side_effect=AssertionError("calculation")),
        TestClient(app) as client,
    ):
        response = client.get("/api/macro-score")

    assert response.status_code == 200, "[RED XBR-601] latest Macro Score cache route is missing"
    assert response.json() == {
        "status": "unavailable",
        "capabilities": {"macro_summary": False},
        "data": None,
    }


def test_latest_macro_score_route_returns_persisted_production_payload_only() -> None:
    as_of = datetime(2026, 9, 27, 12, tzinfo=timezone.utc)
    result = build_macro_score_result(as_of, as_of + timedelta(seconds=5))
    insert_macro_score_snapshot(
        result,
        calculation_cycle_id="route-cycle",
        origin=SnapshotOrigin.PRODUCTION_REFRESH,
    )
    persisted = query_latest_macro_score()
    assert persisted is not None

    with (
        patch("provider_http.get_provider_response", side_effect=AssertionError("provider call")),
        patch("macro_score.sources.load_macro_score_sources", side_effect=AssertionError("input load")),
        patch("macro_score.calculator.aggregate_macro_score", side_effect=AssertionError("calculation")),
        TestClient(app) as client,
    ):
        response = client.get("/api/macro-score")

    assert response.status_code == 200
    assert response.json() == {
        "status": "ok",
        "capabilities": {"macro_summary": False},
        "data": persisted.payload,
    }


def test_macro_score_history_route_returns_latest_snapshot_per_month_in_range() -> None:
    august = datetime(2026, 8, 31, 15, tzinfo=timezone.utc)
    september = datetime(2026, 9, 30, 15, tzinfo=timezone.utc)
    insert_macro_score_snapshot(
        build_macro_score_result(august, august + timedelta(seconds=5)),
        calculation_cycle_id="august-first",
        origin=SnapshotOrigin.PRODUCTION_REFRESH,
    )
    insert_macro_score_snapshot(
        build_macro_score_result(august, august + timedelta(days=1), current_shift=0.5),
        calculation_cycle_id="august-revision",
        origin=SnapshotOrigin.PRODUCTION_REFRESH,
    )
    insert_macro_score_snapshot(
        build_macro_score_result(september, september + timedelta(seconds=5)),
        calculation_cycle_id="september-first",
        origin=SnapshotOrigin.PRODUCTION_REFRESH,
    )

    with TestClient(app) as client:
        response = client.get(
            "/api/macro-score/history",
            params={"start": "2026-08", "end": "2026-09"},
        )

    assert response.status_code == 200
    payload = response.json()
    assert payload["status"] == "ok"
    assert [item["score_month"] for item in payload["data"]] == ["2026-08", "2026-09"]
    assert [item["calculation_cycle_id"] for item in payload["data"]] == [
        "august-revision",
        "september-first",
    ]


def test_empty_macro_score_history_is_an_empty_cache_result() -> None:
    with TestClient(app) as client:
        response = client.get(
            "/api/macro-score/history",
            params={"start": "2026-08", "end": "2026-09"},
        )

    assert response.status_code == 200
    assert response.json() == {"status": "unavailable", "data": []}
