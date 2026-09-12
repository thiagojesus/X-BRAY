from unittest.mock import patch

import pytest

from refresh import RefreshDependencies, RefreshInProgressError, daily_refresh


def _dependencies(payload: dict) -> RefreshDependencies:
    return RefreshDependencies(
        fetch_sgs_batch=lambda *_args, **_kwargs: payload,
        fetch_all_focus=lambda **_kwargs: payload,
        fetch_anbima_ima=lambda **_kwargs: payload,
        fetch_di_curves=lambda **_kwargs: payload,
        fetch_state_polls=lambda **_kwargs: payload,
        refresh_treasury_data=lambda: payload,
    )


def test_live_refresh_lease_returns_conflict() -> None:
    with patch("refresh.acquire_refresh_lease", return_value=False):
        with pytest.raises(RefreshInProgressError):
            daily_refresh(force=True, dependencies=_dependencies({"data": [1]}))


def test_provider_failure_is_sanitized_and_other_tasks_complete() -> None:
    calls = 0

    def sgs(*_args, **_kwargs):
        nonlocal calls
        calls += 1
        if calls == 1:
            raise RuntimeError("payload-with-secret")
        return {"series": [{"data": "01/01/2026", "valor": "1"}]}

    dependencies = _dependencies({"data": [1]})
    dependencies = RefreshDependencies(
        fetch_sgs_batch=sgs,
        fetch_all_focus=dependencies.fetch_all_focus,
        fetch_anbima_ima=dependencies.fetch_anbima_ima,
        fetch_di_curves=dependencies.fetch_di_curves,
        fetch_state_polls=dependencies.fetch_state_polls,
        refresh_treasury_data=dependencies.refresh_treasury_data,
    )
    result = daily_refresh(force=True, dependencies=dependencies)
    assert result["status"] == "partial"
    assert len(result["tasks"]) == 14
    failed = next(task for task in result["tasks"] if task["status"] == "error")
    assert failed["error_code"] == "RuntimeError"
    assert "payload-with-secret" not in str(result)
