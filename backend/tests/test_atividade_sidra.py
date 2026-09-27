from pathlib import Path
from unittest.mock import patch

import pytest
from fastapi.testclient import TestClient

from datafetchers.ibge_sidra import SIDRA_DATASETS
from db import store
from main import app


@pytest.fixture(autouse=True)
def isolated_database(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    if store._conn is not None:
        store._conn.close()
    store._conn = None
    monkeypatch.setattr(store, "DB_PATH", tmp_path / "atividade-sidra.db")
    monkeypatch.setattr(store, "DATABASE_URL", "")
    store.init_db()
    yield
    if store._conn is not None:
        store._conn.close()
    store._conn = None


def _observation(key: str, value: float) -> dict[str, object]:
    spec = SIDRA_DATASETS[key]
    return {
        "dataset_key": key,
        "table_id": spec.table_id,
        "variable_id": spec.variable_id,
        "classification_id": spec.classification_id,
        "category_id": spec.category_id,
        "territory_level": spec.territory_level,
        "territory_code": spec.territory_code,
        "reference_period": "202507",
        "reference_label": "mai-jun-jul 2025" if spec.frequency == "moving_quarter" else "julho 2025",
        "value": value,
        "unit": spec.unit,
        "base_version": spec.base_version,
        "source_published_at": None,
    }


def _seed(keys: tuple[str, ...] = tuple(SIDRA_DATASETS)) -> None:
    store.insert_sidra_observations(
        [_observation(key, float(index + 100)) for index, key in enumerate(keys)],
        first_seen_at="2026-09-27T12:00:00+00:00",
    )


def test_get_sidra_is_cache_only_and_returns_all_five_datasets() -> None:
    _seed()

    with patch("datafetchers.ibge_sidra.httpx.get") as provider_get:
        response = TestClient(app).get("/api/atividade/sidra")

    provider_get.assert_not_called()
    assert response.status_code == 200
    body = response.json()
    assert body["source"] == "IBGE SIDRA"
    assert body["status"] == "ok"
    assert set(body["data"]) == set(SIDRA_DATASETS)
    assert body["unavailable"] == []
    assert body["metadata"]["pim_industria"]["table_id"] == 8888
    assert body["metadata"]["pim_industria"]["variable_id"] == 12607
    assert body["metadata"]["massa_rendimento_real"]["frequency"] == "moving_quarter"
    assert body["metadata"]["massa_rendimento_real"]["period_label"] == "mai-jun-jul 2025"


def test_any_usable_subset_returns_partial_without_disabling_sgs() -> None:
    _seed(("pim_industria",))

    sidra_response = TestClient(app).get("/api/atividade/sidra")

    assert sidra_response.status_code == 200
    body = sidra_response.json()
    assert body["status"] == "partial"
    assert set(body["data"]) == {"pim_industria"}
    assert set(body["unavailable"]) == set(SIDRA_DATASETS) - {"pim_industria"}


def test_empty_sidra_cache_returns_typed_503_without_network() -> None:
    with patch("datafetchers.ibge_sidra.httpx.get") as provider_get:
        response = TestClient(app).get("/api/atividade/sidra")

    provider_get.assert_not_called()
    assert response.status_code == 503
    assert response.json() == {
        "error": "dataset_unavailable",
        "dataset": "atividade_sidra",
    }


def test_sidra_refresh_route_persists_fixture_results() -> None:
    payload = {key: [_observation(key, float(index + 100))] for index, key in enumerate(SIDRA_DATASETS)}
    with patch("routes.atividade_sidra.refresh_sidra_data", return_value=payload) as refresh:
        response = TestClient(app).post("/api/atividade/sidra/refresh")

    assert response.status_code == 200
    assert response.json()["status"] == "refreshed"
    refresh.assert_called_once_with()


def test_sidra_refresh_reuses_shared_bearer_auth(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("XBRAY_ENV", "production")
    monkeypatch.setenv("XBRAY_REFRESH_TOKEN", "server-secret")

    rejected = TestClient(app).post("/api/atividade/sidra/refresh")
    with patch("routes.atividade_sidra.refresh_sidra_data", return_value={"pim_industria": [_observation("pim_industria", 100.0)]}):
        accepted = TestClient(app).post(
            "/api/atividade/sidra/refresh",
            headers={"Authorization": "Bearer server-secret"},
        )

    assert rejected.status_code == 401
    assert accepted.status_code == 200
