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


def test_macro_score_runs_after_derived_with_a_later_cutoff_and_same_cycle() -> None:
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
    assert macro_identity[0] >= derived_identity[0]
    assert macro_identity[1] == derived_identity[1]
    assert result["status"] == "ok"
    assert len(result["tasks"]) == 22
    assert next(task for task in result["tasks"] if task["name"] == "macro_score") == {
        "name": "macro_score",
        "status": "ok",
        "error_code": None,
    }


def test_macro_score_cutoff_includes_derived_snapshot_persisted_in_the_same_cycle() -> None:
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
    assert component["available_at"] == derived_created_at.isoformat()
    assert datetime.fromisoformat(snapshot.payload["as_of"]) >= derived_created_at
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
