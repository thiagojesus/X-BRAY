from pathlib import Path
from unittest.mock import patch

import pytest

from config import ACTIVITY_REFRESH
from datafetchers import bcb_sgs
from refresh import RefreshDependencies, RefreshInProgressError, daily_refresh


EXTERNAL_CODES = {
    "conta_corrente_12m_pib": 23079,
    "idp_12m_pib": 23080,
    "idp_mensal_usd": 22885,
    "balanca_comercial_usd": 22707,
    "investimento_carteira_usd": 22905,
    "reservas_internacionais": 3546,
}


def _dependencies(payload: dict) -> RefreshDependencies:
    return RefreshDependencies(
        fetch_sgs_batch=lambda *_args, **_kwargs: payload,
        fetch_all_focus=lambda **_kwargs: payload,
        fetch_anbima_ima=lambda **_kwargs: payload,
        fetch_di_curves=lambda **_kwargs: payload,
        fetch_state_polls=lambda **_kwargs: payload,
        refresh_treasury_data=lambda: payload,
        refresh_sidra_data=lambda: payload,
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
        refresh_sidra_data=dependencies.refresh_sidra_data,
    )
    result = daily_refresh(force=True, dependencies=dependencies)
    assert result["status"] == "partial"
    assert len(result["tasks"]) == 18
    failed = next(task for task in result["tasks"] if task["status"] == "error")
    assert failed["error_code"] == "RuntimeError"
    assert "payload-with-secret" not in str(result)


def test_red_xbr_301_daily_refresh_deduplicates_reserves_ownership() -> None:
    series_calls: list[int] = []
    batch_calls: list[dict[str, int]] = []

    def fetch_series(
        code: int,
        start_date: str | None = None,
        end_date: str | None = None,
        use_cache: bool = True,
    ) -> list[dict[str, str]]:
        del start_date, end_date, use_cache
        series_calls.append(code)
        return [{"data": "01/08/2026", "valor": "1.0"}]

    def fetch_batch(
        codes: dict[str, int],
        start_date: str | None = None,
        use_cache: bool = True,
    ) -> dict[str, object]:
        batch_calls.append(codes)
        return bcb_sgs.fetch_sgs_batch(codes, start_date=start_date, use_cache=use_cache)

    payload = {"data": [{"value": 1}]}
    dependencies = RefreshDependencies(
        fetch_sgs_batch=fetch_batch,
        fetch_all_focus=lambda **_kwargs: payload,
        fetch_anbima_ima=lambda **_kwargs: payload,
        fetch_di_curves=lambda **_kwargs: payload,
        fetch_state_polls=lambda **_kwargs: payload,
        refresh_treasury_data=lambda: payload,
        refresh_sidra_data=lambda: payload,
    )

    with patch("datafetchers.bcb_sgs.fetch_sgs_series", side_effect=fetch_series):
        result = daily_refresh(force=True, dependencies=dependencies)

    assert EXTERNAL_CODES in batch_calls, "[RED XBR-301] missing sgs_setor_externo ownership"
    assert series_calls.count(3546) == 1, "[RED XBR-301] SGS 3546 must be fetched exactly once"
    assert all(3546 not in codes.values() for codes in batch_calls if codes != EXTERNAL_CODES)
    external_tasks = [task for task in result["tasks"] if task["name"] == "sgs_setor_externo"]
    assert external_tasks == [{"name": "sgs_setor_externo", "status": "ok", "error_code": None}]


def test_xbr_401_activity_refresh_owns_legacy_and_sectoral_series() -> None:
    batch_calls: list[dict[str, int]] = []

    def fetch_batch(
        codes: dict[str, int],
        start_date: str | None = None,
        use_cache: bool = True,
    ) -> dict[str, object]:
        del start_date, use_cache
        batch_calls.append(codes)
        return {name: [{"data": "01/08/2026", "valor": "1.0"}] for name in codes}

    payload = {"data": [{"value": 1}]}
    result = daily_refresh(
        force=True,
        dependencies=RefreshDependencies(
            fetch_sgs_batch=fetch_batch,
            fetch_all_focus=lambda **_kwargs: payload,
            fetch_anbima_ima=lambda **_kwargs: payload,
            fetch_di_curves=lambda **_kwargs: payload,
            fetch_state_polls=lambda **_kwargs: payload,
            refresh_treasury_data=lambda: payload,
            refresh_sidra_data=lambda: payload,
        ),
    )

    assert batch_calls.count(ACTIVITY_REFRESH) == 1
    assert len(ACTIVITY_REFRESH) == 10
    assert next(task for task in result["tasks"] if task["name"] == "sgs_atividade") == {
        "name": "sgs_atividade",
        "status": "ok",
        "error_code": None,
    }


def test_xbr_402_daily_refresh_registers_sidra_task() -> None:
    calls = 0

    def refresh_sidra() -> dict[str, list[dict[str, str]]]:
        nonlocal calls
        calls += 1
        return {f"dataset-{index}": [{"reference_period": "202607"}] for index in range(5)}

    dependencies = _dependencies({"data": [{"value": 1}]})
    dependencies = RefreshDependencies(
        fetch_sgs_batch=dependencies.fetch_sgs_batch,
        fetch_all_focus=dependencies.fetch_all_focus,
        fetch_anbima_ima=dependencies.fetch_anbima_ima,
        fetch_di_curves=dependencies.fetch_di_curves,
        fetch_state_polls=dependencies.fetch_state_polls,
        refresh_treasury_data=dependencies.refresh_treasury_data,
        refresh_sidra_data=refresh_sidra,
    )

    result = daily_refresh(force=True, dependencies=dependencies)

    assert calls == 1
    assert next(task for task in result["tasks"] if task["name"] == "ibge_sidra") == {
        "name": "ibge_sidra",
        "status": "ok",
        "error_code": None,
    }


def test_red_xbr_003_zero_observation_payload_does_not_record_success(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import db.store as store

    if store._conn is not None:
        store._conn.close()
    store._conn = None
    monkeypatch.setattr(store, "DB_PATH", tmp_path / "zero-observations.db")
    monkeypatch.setattr(store, "DATABASE_URL", "")
    store.init_db()

    try:
        result = daily_refresh(force=True, dependencies=_dependencies({"malformed": 1}))
        tasks = store.list_refresh_tasks()

        assert result["status"] == "error"
        assert all(task["status"] == "error" for task in result["tasks"])
        assert all(task["last_success"] is None for task in tasks)
        assert store.get_meta("last_successful_update") is None
        assert store._query("SELECT COUNT(*) AS count FROM observation_vintages") == [{"count": 0}]
    finally:
        if store._conn is not None:
            store._conn.close()
        store._conn = None
