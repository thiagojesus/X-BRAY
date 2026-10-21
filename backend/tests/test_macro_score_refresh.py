from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import patch

import pytest

from derived.models import DerivedInput, DerivedResult, SnapshotOrigin
from derived.refresh import DerivedRefreshError
from derived.repository import insert_snapshot
from macro_score.refresh import refresh_macro_score
from macro_score.repository import query_latest_macro_score
from refresh import RefreshDependencies, daily_refresh
from tests.real_rate_support import isolated_database

pytestmark = pytest.mark.usefixtures(isolated_database.__name__)


def _insert_current_di_snapshots(as_of: datetime, cycle_id: str) -> None:
    for key, value, unit in (
        ("juro_real_ex_ante_1y", 8.2, "pct_annual"),
        ("inclinacao_di_5y_1y", -0.2, "pp"),
    ):
        result = DerivedResult(
            key=key, value=value, unit=unit, as_of=as_of, methodology_version="1.0.0",
            formula="verified integration fixture", inputs=(DerivedInput(
                key="source", value=value, unit=unit, reference_period="2026-10-20",
                available_at=as_of, source="isolated integration fixture",
            ),), reason=None, reference_date="2026-10-20",
        )
        insert_snapshot(
            result, calculation_cycle_id=cycle_id, origin=SnapshotOrigin.PRODUCTION_REFRESH,
            created_at=datetime.now(timezone.utc),
        )


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


def test_red_xbr_601_macro_score_coordinator_persists_production_snapshot() -> None:
    coordinator_path = Path(__file__).parents[1] / "macro_score" / "refresh.py"
    assert coordinator_path.is_file(), (
        "[RED XBR-601] Macro Score production refresh coordinator is missing"
    )

    from macro_score.refresh import refresh_macro_score

    as_of = datetime(2026, 9, 30, 23, 30, tzinfo=timezone.utc)

    inserted = refresh_macro_score(as_of, "cycle-601")
    snapshot = query_latest_macro_score()

    assert inserted == 1
    assert snapshot is not None
    assert snapshot.payload["as_of"] == as_of.isoformat()
    assert snapshot.payload["score_month"] == "2026-09"
    assert snapshot.calculation_cycle_id == "cycle-601"
    assert snapshot.origin is SnapshotOrigin.PRODUCTION_REFRESH


def test_red_xbr_601_refresh_dependencies_expose_macro_score_coordinator() -> None:
    assert "refresh_macro_score" in RefreshDependencies.__dataclass_fields__, (
        "[RED XBR-601] daily refresh has no injectable Macro Score coordinator"
    )


def test_source_loading_failure_does_not_persist_macro_score_snapshot() -> None:
    coordinator_path = Path(__file__).parents[1] / "macro_score" / "refresh.py"
    assert coordinator_path.is_file(), (
        "[RED XBR-601] Macro Score production refresh coordinator is missing"
    )

    from macro_score.refresh import refresh_macro_score

    with (
        patch(
            "macro_score.refresh.load_macro_score_sources",
            side_effect=RuntimeError("private source failure"),
        ),
        pytest.raises(RuntimeError, match="private source failure"),
    ):
        refresh_macro_score(datetime(2026, 9, 30, 12, tzinfo=timezone.utc), "cycle-601")

    assert query_latest_macro_score() is None


def test_refresh_loads_verified_di_registry_and_persists_eligible_component_history() -> None:
    import json

    from db import store
    from tests.test_macro_score_retrospective import _di_sources
    from tests.test_macro_score_sources import _VintageSeed, _insert_vintage

    as_of = datetime(2026, 10, 21, tzinfo=timezone.utc)
    sources = _di_sources(24)
    for observation in sources.retrospective_di:
        store.execute_change(
            "INSERT INTO retrospective_di_observations "
            "(trade_date,symbol,maturity,rate,source_hash,fetched_at,revision,source_basis,source_name,source_member,maturity_basis,security_id) "
            "VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
            (observation.trade_date.isoformat(), observation.symbol, observation.maturity.isoformat(),
             observation.rate, observation.source_hash, observation.fetched_at.isoformat(), "revision",
             observation.source_basis, observation.source_name, observation.source_member,
             "legacy_local_unverified", observation.security_id),
        )
    for instrument in sources.di_instrument_snapshots:
        store.execute_change(
            "INSERT INTO b3_di_instrument_snapshots "
            "(snapshot_date,report_date,symbol,security_id,fin_instrm_id,maturity,trading_start,trading_end,source_hash,source_name,message_definition,message_id,captured_at,available_at,source_basis) "
            "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (instrument.report_date.isoformat(), instrument.report_date.isoformat(), instrument.symbol,
             instrument.security_id, instrument.fin_instrm_id, instrument.maturity.isoformat(),
             instrument.trading_start.isoformat(), instrument.trading_end.isoformat(), instrument.source_hash,
             instrument.source_name, instrument.message_definition, instrument.message_id,
             instrument.captured_at.isoformat(), instrument.available_at.isoformat(), instrument.source_basis),
        )
    for vintage in sources.vintages:
        _insert_vintage(_VintageSeed(
            "focus", vintage.series_key, vintage.reference_period.isoformat(),
            {"Indicador": "IPCA", "Data": vintage.reference_period.isoformat(), "Mediana": vintage.value,
             "Suavizada": "S", "baseCalculo": 0}, vintage.available_at,
        ))

    _insert_current_di_snapshots(as_of, "di-loader-integration")
    assert refresh_macro_score(as_of, "di-loader-integration") == 1
    snapshot = query_latest_macro_score()

    assert snapshot is not None
    for key in ("juro_real_ex_ante_1y", "inclinacao_di_5y_1y"):
        component = snapshot.payload["components"][key]
        assert component["history_count"] >= 24
        assert component["percentile"] is not None, json.dumps(component)
        assert component["score"] is not None
        assert component["reason"] is None
    assert snapshot.payload["dimensions"]["monetary"]["score"] is not None
    from fastapi.testclient import TestClient
    from main import app

    with TestClient(app) as client:
        response = client.get("/api/macro-score")
    assert response.status_code == 200
    payload = response.json()["data"]
    assert payload["calculation_cycle_id"] == "di-loader-integration"
    assert payload["components"]["juro_real_ex_ante_1y"]["history_count"] >= 24
    assert payload["components"]["inclinacao_di_5y_1y"]["percentile"] is not None
    assert payload["dimensions"]["monetary"]["score"] is not None


def test_refresh_does_not_count_registry_snapshots_published_after_cutoff() -> None:
    from db import store
    from tests.test_macro_score_retrospective import _di_sources

    sources = _di_sources(24)
    for instrument in sources.di_instrument_snapshots:
        store.execute_change(
            "INSERT INTO b3_di_instrument_snapshots "
            "(snapshot_date,report_date,symbol,security_id,fin_instrm_id,maturity,trading_start,trading_end,source_hash,source_name,message_definition,message_id,captured_at,available_at,source_basis) "
            "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (instrument.report_date.isoformat(), instrument.report_date.isoformat(), instrument.symbol,
             instrument.security_id, instrument.fin_instrm_id, instrument.maturity.isoformat(),
             instrument.trading_start.isoformat(), instrument.trading_end.isoformat(), instrument.source_hash,
             instrument.source_name, instrument.message_definition, instrument.message_id,
             "2026-10-22T00:00:00+00:00", "2026-10-22T00:00:00+00:00", instrument.source_basis),
        )
    _insert_current_di_snapshots(datetime(2026, 10, 21, tzinfo=timezone.utc), "di-cutoff-integration")
    assert refresh_macro_score(datetime(2026, 10, 21, tzinfo=timezone.utc), "di-cutoff-integration") == 1
    snapshot = query_latest_macro_score()
    assert snapshot is not None
    assert snapshot.payload["components"]["juro_real_ex_ante_1y"]["history_count"] == 1
    assert snapshot.payload["components"]["juro_real_ex_ante_1y"]["reason"] == "insufficient_history"


def test_macro_score_runs_after_derived_with_the_same_cutoff_and_cycle() -> None:
    source_calls = 0
    events: list[str] = []
    derived_identity: tuple[datetime, str] | None = None
    macro_identity: tuple[datetime, str] | None = None

    def source(*_args: object, **_kwargs: object) -> dict[str, list[dict[str, int]]]:
        nonlocal source_calls
        source_calls += 1
        return _payload()

    def observe() -> int:
        events.append("observe")
        return 11

    def derive(as_of: datetime, cycle_id: str) -> int:
        nonlocal derived_identity
        derived_identity = (as_of, cycle_id)
        events.append("derive")
        return 6

    def macro_score(as_of: datetime, cycle_id: str) -> int:
        nonlocal macro_identity
        macro_identity = (as_of, cycle_id)
        events.append("macro_score")
        return 1

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
            refresh_macro_score=macro_score,
        ),
    )

    assert source_calls == 19
    assert events == ["observe", "derive", "macro_score"]
    assert derived_identity is not None
    assert macro_identity is not None
    assert macro_identity[0] == derived_identity[0]
    assert macro_identity[1] == derived_identity[1]
    assert result["status"] == "ok"
    assert len(result["tasks"]) == 22
    assert next(task for task in result["tasks"] if task["name"] == "macro_score") == {
        "name": "macro_score",
        "status": "ok",
        "error_code": None,
    }


def test_macro_score_consumes_same_cycle_derived_snapshot_created_after_cutoff_without_lookahead() -> None:
    # Given
    derived_created_at: datetime | None = None
    derived_cycle_id: str | None = None

    def persist_derived(as_of: datetime, cycle_id: str) -> int:
        nonlocal derived_created_at, derived_cycle_id
        derived_created_at = datetime.now(timezone.utc)
        derived_cycle_id = cycle_id
        result = DerivedResult(
            key="juro_real_ex_ante_1y",
            value=4.0,
            unit="pct_annual",
            as_of=as_of,
            methodology_version="1.0.0",
            formula="((1 + DI/100) / (1 + IPCA/100) - 1) * 100",
            inputs=(DerivedInput(
                key="expected_inflation",
                value=4.66,
                unit="pct_annual",
                reference_period="2026-09-25",
                available_at=as_of,
                source="Focus IPCA 12m",
            ),),
            reason=None,
            reference_date="2026-09-25",
        )
        insert_snapshot(
            result,
            calculation_cycle_id=cycle_id,
            origin=SnapshotOrigin.PRODUCTION_REFRESH,
            created_at=derived_created_at,
        )
        return 1

    # When
    result = daily_refresh(
        force=True,
        dependencies=_dependencies(
            refresh_derived_snapshots=persist_derived,
            refresh_macro_score=refresh_macro_score,
        ),
    )

    # Then
    snapshot = query_latest_macro_score()
    assert result["status"] == "ok"
    assert snapshot is not None
    assert derived_created_at is not None
    assert derived_cycle_id is not None
    component = snapshot.payload["components"]["juro_real_ex_ante_1y"]
    assert component["value"] == 4.0
    assert component["reason"] == "insufficient_history"
    assert component["available_at"] == snapshot.payload["as_of"]
    assert datetime.fromisoformat(snapshot.payload["as_of"]) < derived_created_at
    assert snapshot.calculation_cycle_id == derived_cycle_id


def test_macro_score_failure_is_sanitized_without_blocking_source_or_derived_success() -> None:
    def fail_macro_score(_as_of: datetime, _cycle_id: str) -> int:
        raise RuntimeError("private macro score detail")

    result = daily_refresh(
        force=True,
        dependencies=_dependencies(refresh_macro_score=fail_macro_score),
    )

    macro_task = next(task for task in result["tasks"] if task["name"] == "macro_score")
    derived_task = next(task for task in result["tasks"] if task["name"] == "derived_snapshots")
    assert result["status"] == "partial"
    assert macro_task == {
        "name": "macro_score",
        "status": "error",
        "error_code": "RuntimeError",
    }
    assert derived_task["status"] == "ok"
    assert "private macro score detail" not in str(result)


def test_derived_failure_does_not_skip_macro_score_refresh() -> None:
    macro_calls = 0

    def fail_derived(_as_of: datetime, _cycle_id: str) -> int:
        raise DerivedRefreshError(2, ("spread_credito_pf_selic",))

    def macro_score(_as_of: datetime, _cycle_id: str) -> int:
        nonlocal macro_calls
        macro_calls += 1
        return 1

    result = daily_refresh(
        force=True,
        dependencies=_dependencies(
            refresh_derived_snapshots=fail_derived,
            refresh_macro_score=macro_score,
        ),
    )

    derived_task = next(task for task in result["tasks"] if task["name"] == "derived_snapshots")
    macro_task = next(task for task in result["tasks"] if task["name"] == "macro_score")
    assert result["status"] == "partial"
    assert derived_task["status"] == "error"
    assert macro_task["status"] == "ok"
    assert macro_calls == 1
