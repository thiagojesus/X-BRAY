from dataclasses import replace
from datetime import date, datetime
from types import SimpleNamespace
from unittest.mock import Mock

import refresh_run_tasks
from db.sgs_refresh_plan import build_sgs_plan
from refresh_run_tasks import _sgs_semantic_contract_conflict, execute_refresh_item


def test_legacy_1207_unit_is_blocked_before_acquisition_but_other_legacy_units_are_not() -> None:
    units = {unit.series_code: unit for unit in build_sgs_plan(date(2026, 10, 28), max_archive_units=0).units}
    assert _sgs_semantic_contract_conflict(
        replace(units[1207], contract_version="acquisition-contract-v1")
    )
    assert not _sgs_semantic_contract_conflict(units[1207])
    assert not _sgs_semantic_contract_conflict(units[432])


def test_legacy_1207_plan_item_finishes_semantic_conflict_without_source_io(monkeypatch) -> None:
    unit = next(
        item for item in build_sgs_plan(date(2026, 10, 28), max_archive_units=0).units
        if item.series_code == 1207
    )
    legacy = replace(
        unit,
        contract_version="acquisition-contract-v1",
        unit="brl_million",
        frequency="quarterly",
        freshness_days=120,
    )
    monkeypatch.setattr(refresh_run_tasks, "sgs_plan_unit_for_task", lambda *_: legacy)
    start = Mock()
    finish = Mock()
    monkeypatch.setattr(refresh_run_tasks, "record_refresh_task_start", start)
    monkeypatch.setattr(refresh_run_tasks, "record_refresh_task_finish", finish)
    fetch = Mock(side_effect=AssertionError("legacy 1207 must not make a source request"))
    dependencies = SimpleNamespace(fetch_sgs_window=fetch)
    snapshot = SimpleNamespace(
        run=SimpleNamespace(manifest_version="daily-refresh-v6", run_id="legacy-run")
    )

    result = execute_refresh_item(
        "sgs_plan_legacy", snapshot, dependencies, datetime.now().astimezone()
    )

    fetch.assert_not_called()
    start.assert_called_once()
    finish.assert_called_once()
    assert result.result["error_code"] == "SGSSemanticContractConflict"
