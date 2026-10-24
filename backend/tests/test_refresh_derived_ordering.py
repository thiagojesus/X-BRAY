from datetime import datetime, timezone

import pytest

from db import store
from derived.refresh import DerivedRefreshError
from refresh import RefreshDependencies, daily_refresh
from tests.real_rate_support import isolated_database

pytestmark = pytest.mark.usefixtures(isolated_database.__name__)


def _payload() -> dict[str, list[dict[str, int]]]:
    return {"data": [{"value": 1}]}


def _dependencies(**overrides: object) -> RefreshDependencies:
    payload = _payload()
    dependencies = {
        "fetch_sgs_batch": lambda *_args, **_kwargs: payload,
        "fetch_all_focus": lambda **_kwargs: payload,
        "fetch_focus_12m": lambda **_kwargs: payload,
        "fetch_anbima_ima": lambda **_kwargs: payload,
        "fetch_di_curves": lambda **_kwargs: payload,
        "fetch_state_polls": lambda **_kwargs: payload,
        "refresh_treasury_data": lambda: payload,
        "refresh_sidra_data": lambda: payload,
        "refresh_derived_snapshots": lambda _as_of, _cycle_id: 6,
        **overrides,
    }
    return RefreshDependencies(**dependencies)


def test_red_xbr_503_observes_target_rules_before_derived_cutoff() -> None:
    assert "observe_inflation_target_rules" in RefreshDependencies.__dataclass_fields__, (
        "[RED XBR-503] daily refresh lacks an injectable target-rule observation phase"
    )

    source_calls = 0
    events: list[str] = []
    first_seen_at: datetime | None = None

    def source(*_args: object, **_kwargs: object) -> dict[str, list[dict[str, int]]]:
        nonlocal source_calls
        source_calls += 1
        return _payload()

    def observe() -> int:
        nonlocal first_seen_at
        assert source_calls == 18
        first_seen_at = datetime.now(timezone.utc)
        events.append("observe")
        return 11

    def derive(as_of: datetime, _cycle_id: str) -> int:
        assert first_seen_at is not None and first_seen_at <= as_of
        events.append("derive")
        return 6

    result = daily_refresh(
        force=True,
        dependencies=_dependencies(
            fetch_sgs_batch=source,
            fetch_all_focus=source,
            fetch_focus_12m=source,
            fetch_anbima_ima=source,
            fetch_di_curves=source,
            fetch_state_polls=source,
            refresh_treasury_data=source,
            refresh_sidra_data=source,
            observe_inflation_target_rules=observe,
            refresh_derived_snapshots=derive,
        ),
    )

    assert events == ["observe", "derive"]
    assert result["status"] == "ok"
    assert next(task for task in result["tasks"] if task["name"] == "inflation_target_rules")["status"] == "ok"


def test_target_observation_failure_is_reported_without_blocking_derived_refresh() -> None:
    derived_calls = 0

    def fail_observation() -> int:
        raise RuntimeError("private failure detail")

    def derive(_as_of: datetime, _cycle_id: str) -> int:
        nonlocal derived_calls
        derived_calls += 1
        return 5

    result = daily_refresh(
        force=True,
        dependencies=_dependencies(
            observe_inflation_target_rules=fail_observation,
            refresh_derived_snapshots=derive,
        ),
    )

    observation = next(task for task in result["tasks"] if task["name"] == "inflation_target_rules")
    derived = next(task for task in result["tasks"] if task["name"] == "derived_snapshots")
    assert result["status"] == "partial"
    assert observation["status"] == "error"
    assert observation["error_code"] == "RuntimeError"
    assert derived["status"] == "ok"
    assert derived_calls == 1
    assert "private failure detail" not in str(result)


def test_aggregate_derived_failure_keeps_successful_insert_count_in_task_status() -> None:
    def partial_failure(_as_of: datetime, _cycle_id: str) -> int:
        raise DerivedRefreshError(3, ("spread_credito_pf_selic",))

    result = daily_refresh(
        force=True,
        dependencies=_dependencies(refresh_derived_snapshots=partial_failure),
    )

    derived_task = next(task for task in result["tasks"] if task["name"] == "derived_snapshots")
    persisted_task = next(task for task in store.list_refresh_tasks() if task["task"] == "derived_snapshots")
    assert result["status"] == "partial"
    assert derived_task["status"] == "error"
    assert derived_task["error_code"] == "DerivedRefreshError"
    assert persisted_task["record_count"] == 3
    assert persisted_task["error"] == "DerivedRefreshError"


def test_unusable_source_refresh_skips_target_observation_and_derivation() -> None:
    events: list[str] = []
    payload = {"error": "provider unavailable"}

    result = daily_refresh(
        force=True,
        dependencies=RefreshDependencies(
            fetch_sgs_batch=lambda *_args, **_kwargs: payload,
            fetch_all_focus=lambda **_kwargs: payload,
            fetch_focus_12m=lambda **_kwargs: payload,
            fetch_anbima_ima=lambda **_kwargs: payload,
            fetch_di_curves=lambda **_kwargs: payload,
            fetch_state_polls=lambda **_kwargs: payload,
            refresh_treasury_data=lambda: payload,
            refresh_sidra_data=lambda: payload,
            observe_inflation_target_rules=lambda: events.append("observe") or 11,
            refresh_derived_snapshots=lambda _as_of, _cycle_id: events.append("derive") or 6,
            refresh_macro_score=lambda _as_of, _cycle_id: events.append("macro_score") or 1,
        ),
    )

    assert result["status"] == "error"
    assert events == []
    assert len(result["tasks"]) == 18


def test_main_wires_target_observation_and_six_indicator_coordinator() -> None:
    from derived.inflation_target_inputs import observe_inflation_target_rules
    from derived.refresh import refresh_all_derived_snapshots
    from macro_score.refresh import refresh_macro_score
    from main import daily_refresh as run_main_refresh

    observed: list[RefreshDependencies] = []

    def capture(*, force: bool, dependencies: RefreshDependencies) -> dict[str, object]:
        del force
        observed.append(dependencies)
        return {"status": "ok", "tasks": []}

    with pytest.MonkeyPatch.context() as monkeypatch:
        monkeypatch.setattr("main.run_daily_refresh", capture)
        run_main_refresh(force=True)

    assert len(observed) == 1
    assert observed[0].observe_inflation_target_rules is observe_inflation_target_rules
    assert observed[0].refresh_derived_snapshots is refresh_all_derived_snapshots
    assert observed[0].refresh_macro_score is refresh_macro_score
