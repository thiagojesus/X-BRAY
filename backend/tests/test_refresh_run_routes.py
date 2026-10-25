import pytest
from fastapi.testclient import TestClient

from refresh import RefreshDependencies
from tests.real_rate_support import isolated_database

pytestmark = pytest.mark.usefixtures(isolated_database.__name__)


def _dependencies() -> RefreshDependencies:
    payload = {"data": [{"value": 1}]}
    return RefreshDependencies(
        fetch_sgs_batch=lambda *_args, **_kwargs: payload,
        fetch_sgs_window=lambda _code, *, start_date, **_kwargs: [{"data": start_date, "valor": "1"}],
        fetch_all_focus=lambda **_kwargs: payload,
        fetch_focus_12m=lambda **_kwargs: payload,
        fetch_anbima_ima=lambda **_kwargs: payload,
        fetch_di_curves=lambda **_kwargs: payload,
        fetch_state_polls=lambda **_kwargs: payload,
        refresh_treasury_data=lambda: payload,
        refresh_sidra_data=lambda: payload,
        refresh_derived_snapshots=lambda _as_of, _cycle_id: 3,
        observe_inflation_target_rules=lambda: 11,
        refresh_macro_score=lambda _as_of, _cycle_id: 1,
    )


def test_run_routes_reconcile_replayed_advance_without_running_next_item(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # Given
    import refresh_run_execution
    from db import store
    from main import app

    calls = 0
    dependencies = _dependencies()
    payload = {"data": [{"value": 1}]}

    def fetch(*_args, **_kwargs):
        nonlocal calls
        calls += 1
        return payload

    dependencies = RefreshDependencies(
        fetch_sgs_batch=fetch,
        fetch_sgs_window=lambda _code, *, start_date, **_kwargs: fetch()["data"],
        fetch_all_focus=fetch,
        fetch_focus_12m=fetch,
        fetch_anbima_ima=fetch,
        fetch_di_curves=fetch,
        fetch_state_polls=fetch,
        refresh_treasury_data=fetch,
        refresh_sidra_data=fetch,
        refresh_derived_snapshots=dependencies.refresh_derived_snapshots,
        observe_inflation_target_rules=dependencies.observe_inflation_target_rules,
        refresh_macro_score=dependencies.refresh_macro_score,
    )
    monkeypatch.setattr(refresh_run_execution, "_dependencies", lambda _value: dependencies)

    with TestClient(app) as client:
        # When
        started = client.post(
            "/api/refresh-runs",
            json={"run_id": "api-run", "cycle_id": "api-cycle", "force": True},
        )
        before_advance = client.get("/api/refresh-runs/api-run")
        first_item = started.json()["items"][0]["item_key"]
        first = client.post(
            "/api/refresh-runs/api-run/advance",
            json={"expected_item_key": first_item},
        )
        replayed = client.post(
            "/api/refresh-runs/api-run/advance",
            json={"expected_item_key": first_item},
        )
        status = client.get("/api/refresh-runs/api-run")

    # Then
    assert started.status_code == 200
    assert before_advance.status_code == 200
    assert before_advance.json()["run"]["updated_at"] == started.json()["run"]["updated_at"]
    assert before_advance.json()["run"]["lease_expires_at"] == started.json()["run"]["lease_expires_at"]
    assert first.status_code == 200
    assert replayed.status_code == 200
    assert status.status_code == 200
    assert calls == 1
    assert replayed.json()["items"][0]["status"] == "succeeded"
    assert replayed.json()["items"][1]["status"] == "pending"
    assert status.json()["run"]["lease_expires_at"] is not None
    assert store.list_refresh_tasks()[0]["status"] == "ok"


def test_run_routes_reject_unknown_runs_and_changed_manifests(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # Given
    import refresh_run_execution
    from main import app

    dependencies = _dependencies()

    with TestClient(app) as client:
        # When
        unknown = client.get("/api/refresh-runs/no-such-run")
        started = client.post(
            "/api/refresh-runs",
            json={"run_id": "manifest-run", "cycle_id": "manifest-cycle", "force": True},
        )
        monkeypatch.setattr(refresh_run_execution, "_dependencies", lambda _value: dependencies)
        changed = client.post(
            "/api/refresh-runs",
            json={"run_id": "manifest-run", "cycle_id": "manifest-cycle", "force": False},
        )

    # Then
    assert unknown.status_code == 404
    assert started.status_code == 200
    assert changed.status_code == 409
    assert changed.json() == {"detail": {"error": "refresh_run_manifest_conflict"}}


def test_run_routes_require_refresh_auth_outside_test_mode(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # Given
    from main import app

    monkeypatch.setenv("XBRAY_ENV", "production")
    monkeypatch.delenv("XBRAY_REFRESH_TOKEN", raising=False)

    with TestClient(app, raise_server_exceptions=False) as client:
        # When
        response = client.post(
            "/api/refresh-runs",
            json={"run_id": "auth-run", "cycle_id": "auth-cycle", "force": False},
        )

    # Then
    assert response.status_code == 503
    assert response.json() == {"detail": "refresh_auth_unconfigured"}
