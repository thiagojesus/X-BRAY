from collections.abc import Iterator
from dataclasses import replace
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

import pytest

from config import IPCA_GROUPS
from datafetchers import bcb_sgs
from datafetchers.bcb_sgs import InvalidSGSPayloadError
from db.refresh_run_models import (
    CycleId,
    RefreshRunStart,
    RefreshRunState,
    RefreshWorkItemSpec,
    RefreshWorkItemStatus,
    RunId,
    WorkItemKey,
)
from db.refresh_runs import create_or_resume_run
from db.store import get_meta, set_meta, upsert_sgs
from refresh import (
    RefreshDependencies,
    ipca_group_window_tasks,
    run_refresh_task_detailed,
    sgs_inflation_window_tasks,
    sgs_interest_window_tasks,
)
from refresh_run_execution import (
    RefreshRunAdvanceRequest,
    RefreshRunStartRequest,
    _manifest,
    advance_refresh_run,
    get_refresh_run,
    start_refresh_run,
)
from refresh_run_tasks import (
    _inflation_window_code,
    _mark_inflation_window_fresh,
    _run_legacy_ipca_group,
)
from tests.real_rate_support import isolated_database

pytestmark = pytest.mark.usefixtures(isolated_database.__name__)


def test_refresh_attempt_clock_returns_timezone_aware_iso_timestamp() -> None:
    from datetime import datetime

    from db.refresh_attempt import utc_now

    timestamp = datetime.fromisoformat(utc_now())
    assert timestamp.tzinfo is not None


def _dependencies() -> RefreshDependencies:
    payload = {"data": [{"value": 1}]}
    return RefreshDependencies(
        fetch_sgs_batch=lambda *_args, **_kwargs: payload,
        fetch_sgs_window=lambda code, start, end, **_kwargs: {
            "selic_meta" if code == 432 else "tr": [
                {"data": start, "valor": "1.0"}, {"data": end, "valor": "1.0"},
            ],
        },
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


def test_repeating_a_completed_item_does_not_execute_the_next_source_task() -> None:
    # Given
    calls = 0
    payload = {"data": [{"value": 1}]}

    def fetch(*_args, **_kwargs):
        nonlocal calls
        calls += 1
        return payload

    dependencies = RefreshDependencies(
        fetch_sgs_batch=fetch,
        fetch_sgs_window=lambda *_args, **_kwargs: fetch()["data"],
        fetch_all_focus=fetch,
        fetch_focus_12m=fetch,
        fetch_anbima_ima=fetch,
        fetch_di_curves=fetch,
        fetch_state_polls=fetch,
        refresh_treasury_data=fetch,
        refresh_sidra_data=fetch,
        refresh_derived_snapshots=lambda _as_of, _cycle_id: 0,
    )
    request = RefreshRunStartRequest(RunId("run-one-source"), CycleId("cycle-one-source"), True)
    started = start_refresh_run(request, dependencies)
    first_item = started.items[0]
    advance = RefreshRunAdvanceRequest(started.run.run_id, first_item.item_key)

    # When
    first_result = advance_refresh_run(advance, dependencies)
    replayed_result = advance_refresh_run(advance, dependencies)

    # Then
    assert calls == 1
    assert first_result.items[0].status is RefreshWorkItemStatus.SUCCEEDED
    assert replayed_result.items[0].status is RefreshWorkItemStatus.SUCCEEDED
    assert replayed_result.items[1].status is RefreshWorkItemStatus.PENDING
    from db.store import _query

    attempts = _query(
        "SELECT run_id, item_key, attempt_number, claim_generation, terminal_outcome "
        "FROM refresh_attempt_lifecycle WHERE run_id = ?",
        (str(started.run.run_id),),
    )
    assert len(attempts) == 1
    assert attempts[0]["attempt_number"] == attempts[0]["claim_generation"] == 1
    assert attempts[0]["terminal_outcome"] == "completed"
    assert _query(
        "SELECT COUNT(*) AS count FROM refresh_attempt_milestones WHERE attempt_id = "
        "(SELECT attempt_id FROM refresh_attempt_lifecycle WHERE run_id = ?)",
        (str(started.run.run_id),),
    )[0]["count"] >= 2


def test_v5_refresh_manifest_splits_inflation_interest_and_ipca_into_bounded_windows() -> None:
    # Given
    started = start_refresh_run(
        RefreshRunStartRequest(RunId("windowed-groups"), CycleId("windowed-groups"), True),
        _dependencies(),
    )

    # When
    group_items = [item for item in started.items if item.task.startswith("sgs_ipca_grupos_")]
    inflation_items = [item for item in started.items if item.task.startswith("sgs_inflacao_w")]

    # Then
    assert started.run.manifest_version == "daily-refresh-v5"
    assert len(group_items) == 36
    assert len(inflation_items) == 7 * len(bcb_sgs.sgs_annual_windows("2015-01-01", datetime.now(timezone.utc).date().isoformat()))
    assert len({item.task for item in group_items}) == 36
    assert len({item.task for item in inflation_items}) == len(inflation_items)
    assert all(item.task != "sgs_inflacao" for item in started.items)
    assert all(item.status is RefreshWorkItemStatus.PENDING for item in group_items)
    assert all(item.task != "polymarket" for item in started.items)
    assert sum(item.task.startswith("sgs_juros_w432_") for item in started.items) >= 11
    assert sum(item.task.startswith("sgs_juros_w226_") for item in started.items) >= 11


def test_inflation_window_task_is_a_primary_only_frozen_bounded_request() -> None:
    observed: list[tuple[int, str, str, bool, bool, int]] = []

    def fetch(
        code: int, *, start_date: str, end_date: str, use_cache: bool,
        fail_fast: bool, request_timeout: int,
    ) -> list[dict[str, str]]:
        observed.append((code, start_date, end_date, use_cache, fail_fast, request_timeout))
        return [{"data": start_date, "valor": "1.0"}]

    tasks = sgs_inflation_window_tasks(False, replace(_dependencies(), fetch_sgs_window=fetch), date(2026, 10, 26))
    task = next(task for task in tasks if task.name.startswith("sgs_inflacao_w433_"))
    payload = task.run()

    assert task.name == "sgs_inflacao_w433_2015-01-01_2015-12-31"
    assert observed == [(433, "01/01/2015", "31/12/2015", False, True, 20)]
    assert payload == {"ipca": [{"data": "01/01/2015", "valor": "1.0"}]}


def test_inflation_windows_cover_each_series_without_gaps_and_freeze_as_of() -> None:
    from config import INFLATION

    cutoff = date(2026, 10, 26)
    tasks = sgs_inflation_window_tasks(False, _dependencies(), cutoff)
    expected_windows = bcb_sgs.sgs_annual_windows("2015-01-01", cutoff.isoformat())

    for code in INFLATION.values():
        actual = [task.name for task in tasks if task.name.startswith(f"sgs_inflacao_w{code}_")]
        expected = [
            f"sgs_inflacao_w{code}_{date(int(start[6:]), int(start[3:5]), int(start[:2])).isoformat()}_"
            f"{date(int(end[6:]), int(end[3:5]), int(end[:2])).isoformat()}"
            for start, end in expected_windows
        ]
        assert actual == expected
    assert tasks[-1].name.endswith("2026-01-01_2026-10-26")


def test_v5_resume_retries_only_failed_inflation_child_from_frozen_manifest() -> None:
    from db import store

    calls: list[tuple[int, str, str, bool, int]] = []

    def fetch(
        code: int, *, start_date: str, end_date: str, use_cache: bool,
        fail_fast: bool, request_timeout: int,
    ) -> list[dict[str, str]]:
        assert fail_fast is True
        calls.append((code, start_date, end_date, use_cache, request_timeout))
        return [{"data": start_date, "valor": "1.0"}]

    dependencies = replace(_dependencies(), fetch_sgs_window=fetch)
    request = RefreshRunStartRequest(RunId("v5-resume-failed-inflation"), CycleId("v5-resume-failed-inflation"), False)
    started = start_refresh_run(request, dependencies)
    target_index = next(index for index, item in enumerate(started.items) if item.task.startswith("sgs_inflacao_w433_"))
    target = started.items[target_index]
    for item in started.items[:target_index]:
        store._execute(
            "UPDATE refresh_work_items SET status='succeeded', attempt=1, claimed_at=?, finished_at=?, "
            "series_updated=1, record_count=1 WHERE run_id=? AND item_key=?",
            (started.run.created_at, started.run.created_at, started.run.run_id, item.item_key),
        )
    store._execute(
        "UPDATE refresh_work_items SET status='failed', attempt=1, claimed_at=?, finished_at=?, error_code=? "
        "WHERE run_id=? AND item_key=?",
        (started.run.created_at, started.run.created_at, "TimeoutError", started.run.run_id, target.item_key),
    )

    resumed = start_refresh_run(request, dependencies)
    result = advance_refresh_run(
        RefreshRunAdvanceRequest(started.run.run_id, target.item_key), dependencies
    )

    assert [(item.item_key, item.task, item.task_order) for item in resumed.items] == [
        (item.item_key, item.task, item.task_order) for item in started.items
    ]
    assert calls == [(433, "01/01/2015", "31/12/2015", False, 20)]
    assert result.items[target_index].status is RefreshWorkItemStatus.SUCCEEDED
    assert all(item.status is RefreshWorkItemStatus.PENDING for item in result.items[target_index + 1:])


def test_inflation_freshness_advances_only_after_complete_primary_window_manifest() -> None:
    from db import store

    dependencies = _dependencies()
    request = RefreshRunStartRequest(
        RunId("v5-inflation-freshness"), CycleId("v5-inflation-freshness"), False
    )
    started = start_refresh_run(request, dependencies)
    target = next(item for item in started.items if item.task.startswith("sgs_inflacao_w433_"))
    store._execute(
        "UPDATE refresh_work_items SET status='succeeded', attempt=1, claimed_at=?, finished_at=?, "
        "series_updated=1, record_count=1 WHERE run_id=?",
        (started.run.created_at, started.run.created_at, started.run.run_id),
    )
    snapshot = get_refresh_run(started.run.run_id)
    set_meta("last_successful_update", "")

    _mark_inflation_window_fresh(
        target.task,
        snapshot,
        {"ipca": [{"data": "01/01/2015", "valor": "1.0"}]},
    )

    assert get_meta("sgs_last_refresh_433")
    assert get_meta("last_successful_update")
    assert _inflation_window_code("sgs_inflacao_w433_2015-01-01_2015-12-31") == 433
    assert _inflation_window_code("sgs_inflacao_w999999_2015-01-01_2015-12-31") is None


def test_v4_manifest_remains_monolithic_and_has_no_retired_source() -> None:
    manifest = _manifest(False, _dependencies(), "daily-refresh-v4", date(2026, 10, 26))

    assert any(item.task == "sgs_inflacao" for item in manifest)
    assert not any(item.task.startswith("sgs_inflacao_w") for item in manifest)
    assert any(item.task.startswith("sgs_juros_w226_") for item in manifest)
    assert all(item.task != "polymarket" for item in manifest)


def test_sgs_interest_window_dispatch_uses_injected_adapter_contract() -> None:
    observed: list[tuple[int, str, str, bool, bool, float]] = []

    def fetch(code: int, *, start_date: str, end_date: str, use_cache: bool, fail_fast: bool, request_timeout: float):
        observed.append((code, start_date, end_date, use_cache, fail_fast, request_timeout))
        return [{"data": start_date, "valor": "0"}]

    dependencies = replace(_dependencies(), fetch_sgs_window=fetch)
    item = next(
        task for task in sgs_interest_window_tasks(False, dependencies, date(2015, 12, 31))
        if task.name.startswith("sgs_juros_w432_")
    )
    payload = item.run()
    assert observed == [(432, "01/01/2015", "31/12/2015", True, True, 20)]
    assert payload == {"selic_meta": [{"data": "01/01/2015", "valor": "0"}]}


def test_retired_polymarket_manifest_item_is_terminal_without_provider_call() -> None:
    from refresh_run_tasks import execute_refresh_item

    dependencies = _dependencies()
    calls = 0

    def never_call(**_kwargs):
        nonlocal calls
        calls += 1
        return {"data": []}

    dependencies = replace(dependencies, fetch_state_polls=never_call)
    now = datetime.now(timezone.utc)
    snapshot = create_or_resume_run(RefreshRunStart(
        run_id=RunId("legacy-retired-polymarket"),
        cycle_id=CycleId("legacy-retired-polymarket"),
        force=False,
        manifest_version="daily-refresh-v2",
        derived_cutoff=None,
        macro_cutoff=None,
        items=(RefreshWorkItemSpec(WorkItemKey("polymarket"), "polymarket", 0),),
        now=now.isoformat(),
        lease_expires_at=(now + timedelta(minutes=5)).isoformat(),
    ))

    result = execute_refresh_item("polymarket", snapshot, dependencies, now)

    assert result.result == {"name": "polymarket", "status": "error", "error_code": "retired_source"}
    assert calls == 0


def test_invalid_ipca_window_fails_run_without_overwriting_cached_data_or_freshness() -> None:
    # Given
    from db import store

    series_code = next(iter(IPCA_GROUPS.values()))
    previous_freshness = "2026-10-01T00:00:00+00:00"
    cached_record = {"data": "01/01/2025", "valor": "0.00"}
    upsert_sgs(series_code, [cached_record])
    set_meta(f"sgs_last_refresh_{series_code}", previous_freshness)
    invalid_payload = InvalidSGSPayloadError(
        series_code,
        "01/01/1990",
        "01/01/2000",
        "expected-list",
    )

    def fetch_sgs_batch(codes, **_kwargs):
        if series_code in codes.values():
            raise invalid_payload
        return {name: [cached_record] for name in codes}

    dependencies = replace(_dependencies(), fetch_sgs_batch=fetch_sgs_batch)
    now = datetime.now(timezone.utc)
    legacy_manifest = _manifest(True, dependencies, "daily-refresh-v3", now.date())
    started = create_or_resume_run(RefreshRunStart(
        run_id=RunId("invalid-ipca-window"),
        cycle_id=CycleId("invalid-ipca-window"),
        force=True,
        manifest_version="daily-refresh-v3",
        derived_cutoff=None,
        macro_cutoff=None,
        items=legacy_manifest,
        now=now.isoformat(),
        lease_expires_at=(now + timedelta(minutes=30)).isoformat(),
    ))
    assert started.items[8].task == f"sgs_ipca_grupos_{series_code}_0"
    for item in started.items[:8]:
        advance_refresh_run(RefreshRunAdvanceRequest(started.run.run_id, item.item_key), dependencies)

    # When
    failed = advance_refresh_run(
        RefreshRunAdvanceRequest(started.run.run_id, started.items[8].item_key),
        dependencies,
    )

    # Then
    failed_item = failed.items[8]
    assert failed_item.status is RefreshWorkItemStatus.FAILED
    assert failed_item.error_code == invalid_payload.error_code
    assert get_meta(f"sgs_last_refresh_{series_code}") == previous_freshness
    assert store.query_sgs(series_code) == [{"data": "01/01/2025", "valor": "0.0"}]


def test_fresh_ipca_group_cache_completes_each_v2_window_without_provider_io(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # Given: cached observations in each refresh window and a fresh series watermark.
    series_code = IPCA_GROUPS["educacao"]
    observations = [
        {"data": "01/08/1999", "valor": "0"},
        {"data": "01/02/2005", "valor": "1.2"},
        {"data": "01/02/2015", "valor": "2.5"},
        {"data": "01/02/2025", "valor": "3.1"},
    ]
    upsert_sgs(series_code, observations)
    freshness = datetime.now(timezone.utc).isoformat()
    set_meta(f"sgs_last_refresh_{series_code}", freshness)

    def unexpected_provider_call(*_args, **_kwargs):
        pytest.fail("fresh cached SGS windows must not call the provider")

    monkeypatch.setattr(bcb_sgs, "get_provider_response_bounded", unexpected_provider_call)
    window_payloads = []

    def fetch_from_cache(codes, **options):
        payload = bcb_sgs.fetch_sgs_batch(codes, **options)
        window_payloads.append(payload)
        return payload

    dependencies = replace(_dependencies(), fetch_sgs_batch=fetch_from_cache)
    window_tasks = tuple(
        task
        for task in ipca_group_window_tasks(False, dependencies)
        if task.name.startswith(f"sgs_ipca_grupos_{series_code}_")
    )

    # When: execute the production v2 window tasks through the refresh task boundary.
    executions = [run_refresh_task_detailed(task) for task in window_tasks]

    # Then: every date window reads only its cached rows and reports success.
    assert [task.name for task in window_tasks] == [
        f"sgs_ipca_grupos_{series_code}_{index}" for index in range(4)
    ]
    assert [execution.result["status"] for execution in executions] == ["ok"] * 4
    assert [execution.record_count for execution in executions] == [1, 1, 1, 1]
    assert [execution.series_updated for execution in executions] == [1, 1, 1, 1]
    assert [execution.result["error_code"] for execution in executions] == [None] * 4
    assert window_payloads == [
        {"educacao": [{**observation, "valor": str(float(observation["valor"]))}]}
        for observation in observations
    ]
    assert get_meta(f"sgs_last_refresh_{series_code}") == freshness


def test_expired_legacy_claim_resumes_same_run_and_exact_manifest() -> None:
    # Given
    from db import store

    dependencies = replace(
        _dependencies(),
        fetch_sgs_batch=lambda codes, **_kwargs: {
            "data": [{"value": 1}],
            **{name: [{"value": 1}] for name in codes},
        },
    )
    run_id = RunId("legacy-expired-run")
    cycle_id = CycleId("legacy-expired-run")
    now = datetime.now(timezone.utc)
    legacy_items = _manifest(False, dependencies, "daily-refresh-v1")
    created = create_or_resume_run(
        RefreshRunStart(
            run_id=run_id,
            cycle_id=cycle_id,
            force=False,
            manifest_version="daily-refresh-v1",
            derived_cutoff=None,
            macro_cutoff=None,
            items=legacy_items,
            now=now.isoformat(),
            lease_expires_at=(now + timedelta(minutes=30)).isoformat(),
        )
    )
    for previous_item in created.items[:8]:
        advance_refresh_run(
            RefreshRunAdvanceRequest(run_id, previous_item.item_key),
            dependencies,
        )
    group_item = created.items[8]
    assert group_item.task == "sgs_ipca_grupos"
    expired_at = (now - timedelta(seconds=1)).isoformat()
    store._execute(
        "UPDATE refresh_leases SET expires_at = ? WHERE lock_key = 'daily_refresh'",
        (expired_at,),
    )
    store._execute(
        "UPDATE refresh_work_items SET status='claimed', attempt=1, claim_token='expired-token', "
        "claim_expires_at=?, updated_at=? WHERE run_id=? AND item_key=?",
        (expired_at, expired_at, run_id, group_item.item_key),
    )

    # When
    resumed = start_refresh_run(
        RefreshRunStartRequest(run_id, cycle_id, False),
        dependencies,
    )
    advanced = advance_refresh_run(
        RefreshRunAdvanceRequest(run_id, group_item.item_key),
        dependencies,
    )

    # Then
    assert resumed.run.manifest_version == "daily-refresh-v1"
    assert [(item.item_key, item.task, item.task_order) for item in resumed.items] == [
        (item.item_key, item.task, item.task_order) for item in created.items
    ]
    assert advanced.items[8].attempt == 2
    assert advanced.items[8].task == "sgs_ipca_grupos"
    assert advanced.items[8].status is RefreshWorkItemStatus.SUCCEEDED
    assert all(item.status is RefreshWorkItemStatus.SUCCEEDED for item in advanced.items[:9])


def test_legacy_ipca_manifest_uses_bounded_requests_and_marks_fresh_only_at_end(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: list[dict[str, object]] = []
    metadata: list[tuple[str, str]] = []

    def fetch(codes: dict[str, int], **kwargs: object) -> dict[str, object]:
        calls.append(kwargs)
        name = next(iter(codes))
        return {name: [{"value": len(calls)}]}

    monkeypatch.setattr("refresh_run_tasks.set_meta", lambda key, value: metadata.append((key, value)))

    result = _run_legacy_ipca_group(RefreshDependencies(fetch_sgs_batch=fetch), False)

    assert len(calls) == 36
    assert all(call["request_timeout"] <= 5 for call in calls)
    assert all(call["fail_fast"] is True and call["mark_refreshed"] is False for call in calls)
    assert len(result) == 9
    assert len(metadata) == 10


def test_legacy_ipca_manifest_does_not_mark_fresh_after_partial_failure(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls = 0
    metadata: list[tuple[str, str]] = []

    def fetch(_codes: dict[str, int], **_kwargs: object) -> dict[str, object]:
        nonlocal calls
        calls += 1
        if calls == 2:
            raise TimeoutError("provider timeout")
        return {"alimentacao_bebidas": [{"value": 1}]}

    monkeypatch.setattr("refresh_run_tasks.set_meta", lambda key, value: metadata.append((key, value)))

    with pytest.raises(TimeoutError, match="provider timeout"):
        _run_legacy_ipca_group(RefreshDependencies(fetch_sgs_batch=fetch), False)

    assert calls == 2
    assert metadata == []


def test_resumable_phases_run_after_sources_with_the_same_cutoff_and_cycle() -> None:
    # Given
    source_calls = 0
    events: list[str] = []
    expected_source_calls = 76 + len(
        sgs_inflation_window_tasks(True, _dependencies(), datetime.now(timezone.utc).date())
    )
    derived_identity: tuple[datetime, str] | None = None
    macro_identity: tuple[datetime, str] | None = None
    payload = {"data": [{"value": 1}]}

    def source(*_args, **_kwargs):
        nonlocal source_calls
        source_calls += 1
        return payload

    def source_window(_code: int, *, start_date: str, end_date: str, **_kwargs):
        source()
        return [{"data": start_date, "valor": "1.0"}, {"data": end_date, "valor": "1.0"}]

    def observe() -> int:
        assert source_calls == expected_source_calls
        events.append("observe")
        return 11

    def derive(as_of: datetime, cycle_id: str) -> int:
        nonlocal derived_identity
        derived_identity = (as_of, cycle_id)
        events.append("derive")
        return 6

    def macro(as_of: datetime, cycle_id: str) -> int:
        nonlocal macro_identity
        macro_identity = (as_of, cycle_id)
        events.append("macro")
        return 1

    dependencies = RefreshDependencies(
        fetch_sgs_batch=source,
        fetch_sgs_window=source_window,
        fetch_all_focus=source,
        fetch_focus_12m=source,
        fetch_anbima_ima=source,
        fetch_di_curves=source,
        fetch_state_polls=source,
        refresh_treasury_data=source,
        refresh_sidra_data=source,
        refresh_derived_snapshots=derive,
        observe_inflation_target_rules=observe,
        refresh_macro_score=macro,
    )
    started = start_refresh_run(
        RefreshRunStartRequest(RunId("run-phases"), CycleId("cycle-phases"), True),
        dependencies,
    )

    # When
    snapshot = started
    for item in started.items:
        snapshot = advance_refresh_run(
            RefreshRunAdvanceRequest(started.run.run_id, item.item_key),
            dependencies,
        )

    # Then
    assert source_calls == expected_source_calls
    assert events == ["observe", "derive", "macro"]
    assert derived_identity is not None
    assert macro_identity == derived_identity
    assert snapshot.run.state is RefreshRunState.SUCCEEDED
    assert snapshot.run.derived_cutoff == snapshot.run.macro_cutoff
    assert all(item.status is RefreshWorkItemStatus.SUCCEEDED for item in snapshot.items)
    assert get_refresh_run(started.run.run_id) == snapshot


def test_all_failed_sources_skip_derived_phases_without_advancing_success_marker() -> None:
    # Given
    phase_calls: list[str] = []
    payload = {"error": "provider unavailable"}
    dependencies = RefreshDependencies(
        fetch_sgs_batch=lambda *_args, **_kwargs: payload,
        fetch_sgs_window=lambda *_args, **_kwargs: (_ for _ in ()).throw(RuntimeError("provider unavailable")),
        fetch_all_focus=lambda **_kwargs: payload,
        fetch_focus_12m=lambda **_kwargs: payload,
        fetch_anbima_ima=lambda **_kwargs: payload,
        fetch_di_curves=lambda **_kwargs: payload,
        fetch_state_polls=lambda **_kwargs: payload,
        refresh_treasury_data=lambda: payload,
        refresh_sidra_data=lambda: payload,
        refresh_derived_snapshots=lambda _as_of, _cycle_id: phase_calls.append("derived") or 0,
        observe_inflation_target_rules=lambda: phase_calls.append("observe") or 11,
        refresh_macro_score=lambda _as_of, _cycle_id: phase_calls.append("macro") or 1,
    )
    started = start_refresh_run(
        RefreshRunStartRequest(RunId("run-all-source-failure"), CycleId("cycle-failure"), True),
        dependencies,
    )

    # When
    snapshot = started
    source_item_count = len(started.items) - 3
    for item in started.items[:source_item_count]:
        for _attempt in range(2):
            snapshot = advance_refresh_run(
                RefreshRunAdvanceRequest(started.run.run_id, item.item_key),
                dependencies,
            )
    for item in started.items[source_item_count:]:
        snapshot = advance_refresh_run(
            RefreshRunAdvanceRequest(started.run.run_id, item.item_key),
            dependencies,
        )

    # Then
    from db import store

    assert phase_calls == []
    assert snapshot.run.state is RefreshRunState.FAILED
    assert all(item.status is RefreshWorkItemStatus.FAILED for item in snapshot.items[:source_item_count])
    assert all(item.status is RefreshWorkItemStatus.SKIPPED for item in snapshot.items[source_item_count:])
    assert store.get_meta("last_successful_update") is None


@pytest.fixture
def isolated_database(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    from db import store

    if store._conn is not None:
        store._conn.close()
    store._conn = None
    monkeypatch.setattr(store, "DB_PATH", tmp_path / "refresh-run-execution.db")
    monkeypatch.setattr(store, "DATABASE_URL", "")
    store.init_db()
    yield
    if store._conn is not None:
        store._conn.close()
    store._conn = None
