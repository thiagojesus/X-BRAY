from dataclasses import replace
from datetime import datetime, timedelta, timezone
from importlib import import_module
from typing import Final

import pytest
from fastapi.testclient import TestClient

from derived.models import SnapshotOrigin
from macro_score.calculator import MacroScoreResult
from macro_score.repository import (
    MacroScoreSnapshot,
    insert_macro_score_snapshot,
)
from macro_score.serialization import MacroScorePayload, macro_score_payload
from main import app
from tests.macro_score_support import build_macro_score_result
from tests.real_rate_support import isolated_database

_MONTHS: Final = (8, 9)
_SUMMARY_ROUTE = "/api/macro-score/summary"

pytestmark = pytest.mark.usefixtures(isolated_database.__name__)


def _result(month: int, shift: float = 0.0, cutoff_seconds: int = 0) -> MacroScoreResult:
    last_day = (datetime(2026, month + 1, 1, tzinfo=timezone.utc) - timedelta(days=1)).day
    as_of = datetime(2026, month, last_day, 12, tzinfo=timezone.utc) + timedelta(seconds=cutoff_seconds)
    return build_macro_score_result(as_of, as_of + timedelta(seconds=5), current_shift=shift)


def _persist(result: MacroScoreResult, cycle_id: str, origin: SnapshotOrigin) -> None:
    insert_macro_score_snapshot(
        result,
        calculation_cycle_id=cycle_id,
        origin=origin,
        created_at=result.calculated_at + timedelta(seconds=1),
    )


def _snapshot(result: MacroScoreResult, cycle_id: str) -> MacroScoreSnapshot:
    return MacroScoreSnapshot(
        payload=macro_score_payload(
            result,
            calculation_cycle_id=cycle_id,
            origin=SnapshotOrigin.PRODUCTION_REFRESH,
        ),
        inputs_hash=result.input_fingerprint,
        calculation_cycle_id=cycle_id,
        origin=SnapshotOrigin.PRODUCTION_REFRESH,
        created_at=result.calculated_at + timedelta(seconds=1),
    )


def _insert_pair(previous_shift: float, current_shift: float) -> None:
    for month, shift, cycle in zip(_MONTHS, (previous_shift, current_shift), ("previous", "current"), strict=True):
        _persist(_result(month, shift), cycle, SnapshotOrigin.PRODUCTION_REFRESH)


def test_latest_response_advertises_summary_disabled_when_environment_is_unset(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # Given
    monkeypatch.delenv("XBRAY_ENABLE_MACRO_SUMMARY", raising=False)

    # When
    with TestClient(app) as client:
        response = client.get("/api/macro-score")

    # Then
    assert response.status_code == 200
    assert response.json()["capabilities"] == {"macro_summary": False}


def test_summary_disabled_returns_404_without_loading_snapshots(monkeypatch: pytest.MonkeyPatch) -> None:
    # Given
    monkeypatch.setenv("XBRAY_ENABLE_MACRO_SUMMARY", "false")
    route_module = import_module("routes.macro_score")
    monkeypatch.setattr(
        route_module,
        "query_latest_macro_score_pair",
        lambda: pytest.fail("disabled summary must not read snapshot history"),
        raising=False,
    )

    # When
    with TestClient(app) as client:
        response = client.get(_SUMMARY_ROUTE)

    # Then
    assert response.status_code == 404
    assert response.json() == {"error": "summary_disabled"}


def test_summary_enabled_requires_two_distinct_production_months(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # Given
    monkeypatch.setenv("XBRAY_ENABLE_MACRO_SUMMARY", "true")
    august = _result(8)
    _persist(august, "august-first", SnapshotOrigin.PRODUCTION_REFRESH)
    _persist(
        replace(august, calculated_at=august.calculated_at + timedelta(hours=1), input_fingerprint="august-revision"),
        "august-revision",
        SnapshotOrigin.PRODUCTION_REFRESH,
    )
    _persist(_result(9), "fixture-september", SnapshotOrigin.TEST_FIXTURE)

    # When
    with TestClient(app) as client:
        response = client.get(_SUMMARY_ROUTE)

    # Then
    assert response.status_code == 409
    assert response.json() == {"error": "insufficient_equivalent_cycles"}


def test_summary_uses_latest_snapshots_from_distinct_months_and_breaks_dimension_ties_alphabetically(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # Given
    monkeypatch.setenv("XBRAY_ENABLE_MACRO_SUMMARY", "true")
    _insert_pair(-12.0, -12.0)
    _persist(
        _result(8, 0.0, 60),
        "august-latest-revision",
        SnapshotOrigin.PRODUCTION_REFRESH,
    )

    # When
    with TestClient(app) as client:
        response = client.get(_SUMMARY_ROUTE)

    # Then
    assert response.status_code == 200
    payload = response.json()["data"]
    assert payload["previous_score_month"] == "2026-08"
    assert payload["current_score_month"] == "2026-09"
    assert payload["previous_score"] == pytest.approx(44.675925925925924)
    assert payload["current_score"] == pytest.approx(50.23148148148148)
    assert payload["score_delta"] == pytest.approx(5.555555555555557)
    assert payload["direction"] == "more_favorable"
    assert [item["key"] for item in payload["improving_dimensions"]] == ["credit", "inflation"]
    assert [item["key"] for item in payload["worsening_dimensions"]] == ["external", "activity"]
    assert payload["summary"] == (
        "Macro Score ficou 5,556 pontos mais favorável entre 2026-08 e 2026-09. "
        "Maiores variações favoráveis: Crédito (+50,000 pontos), Inflação (+50,000 pontos). "
        "Maiores variações desfavoráveis: Externo (-50,000 pontos), Atividade (-16,667 pontos). "
        "Variação observada, sem atribuição causal."
    )


@pytest.mark.parametrize(
    ("previous_shift", "current_shift", "expected_direction"),
    ((0.0, -12.0, "more_favorable"), (-12.0, 0.0, "less_favorable")),
)
def test_summary_templates_include_numeric_positive_and_negative_deltas(
    monkeypatch: pytest.MonkeyPatch,
    previous_shift: float,
    current_shift: float,
    expected_direction: str,
) -> None:
    # Given
    monkeypatch.setenv("XBRAY_ENABLE_MACRO_SUMMARY", "true")
    _insert_pair(previous_shift, current_shift)

    # When
    with TestClient(app) as client:
        response = client.get(_SUMMARY_ROUTE)

    # Then
    assert response.status_code == 200
    payload = response.json()["data"]
    assert payload["direction"] == expected_direction
    assert payload["score_delta"] == pytest.approx(payload["current_score"] - payload["previous_score"])
    assert payload["summary"].endswith("Variação observada, sem atribuição causal.")
    assert len(payload["improving_dimensions"]) <= 2
    assert len(payload["worsening_dimensions"]) <= 2
    assert all("delta" in dimension for dimension in payload["improving_dimensions"])
    assert all("delta" in dimension for dimension in payload["worsening_dimensions"])


def test_summary_rejects_snapshots_with_different_available_component_keys(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # Given
    monkeypatch.setenv("XBRAY_ENABLE_MACRO_SUMMARY", "true")
    previous = _snapshot(_result(8), "previous")
    current = _snapshot(_result(9, -12.0), "current")
    component_key = next(iter(current.payload["components"]))
    reduced_components = {
        key: component
        for key, component in current.payload["components"].items()
        if key != component_key
    }
    reduced_payload: MacroScorePayload = {**current.payload, "components": reduced_components}
    incompatible_current = replace(current, payload=reduced_payload)
    route_module = import_module("routes.macro_score")
    monkeypatch.setattr(
        route_module,
        "query_latest_macro_score_pair",
        lambda: (incompatible_current, previous),
        raising=False,
    )

    # When
    with TestClient(app) as client:
        response = client.get(_SUMMARY_ROUTE)

    # Then
    assert response.status_code == 409
    assert response.json() == {"error": "insufficient_equivalent_cycles"}
