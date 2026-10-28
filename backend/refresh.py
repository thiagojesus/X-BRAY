from collections.abc import Callable, Mapping
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass, replace
from datetime import date, datetime, timedelta, timezone
from operator import length_hint
from threading import Event, RLock, Thread
from time import perf_counter
from typing import Final, TypeAlias, cast
from uuid import uuid4

import httpx

from config import (
    ACTIVITY_REFRESH,
    COMPLEMENTARY_REFRESH,
    CREDIT,
    EXCHANGE,
    FISCAL,
    INFLATION,
    INTEREST_RATES,
    IPCA_CORE,
    IPCA_GROUPS,
    IPCA_NATURE,
    IPCA_PRICES,
    SETOR_EXTERNO,
)
from datafetchers.anbima import fetch_anbima_ima
from datafetchers.b3_di import fetch_di_curves
from datafetchers.bcb_sgs import (
    InvalidSGSPayloadError,
    fetch_sgs_batch,
    fetch_sgs_window,
    sgs_annual_windows,
    sgs_refresh_windows,
)
from datafetchers.focus import fetch_all_focus, fetch_focus_12m
from datafetchers.ibge_sidra import refresh_sidra_data
from datafetchers.tesouro_direto import refresh_treasury_data
from db.store import (
    acquire_refresh_lease,
    heartbeat_refresh_lease,
    record_refresh_task_finish,
    record_refresh_task_start,
    release_refresh_lease,
    set_meta,
)
from derived.refresh import refresh_all_derived_snapshots
from refresh_derived_phase import run_derived_refresh_phase
from refresh_reporting import (
    RefreshResult,
    RefreshTaskExecution,
    RefreshTaskResult,
    TaskStatus,
    refresh_status_payload,
)
from source_ingestion import FetchResult, reset_refresh_context, set_refresh_context

__all__ = ("RefreshResult", "RefreshTaskResult", "TaskStatus", "refresh_status_payload")

RefreshPayload: TypeAlias = Mapping[str, object]
_refresh_treasury_payload: Callable[..., RefreshPayload] = cast(
    Callable[..., RefreshPayload], refresh_treasury_data
)


@dataclass(frozen=True, slots=True)
class RefreshInProgressError(RuntimeError):
    retry_after: int = 60


@dataclass(frozen=True, slots=True)
class RefreshDependencies:
    fetch_sgs_batch: Callable[..., RefreshPayload] = fetch_sgs_batch
    fetch_sgs_window: Callable[..., object] = fetch_sgs_window
    fetch_all_focus: Callable[..., RefreshPayload] = fetch_all_focus
    fetch_focus_12m: Callable[..., RefreshPayload] = fetch_focus_12m
    fetch_anbima_ima: Callable[..., RefreshPayload] = fetch_anbima_ima
    fetch_di_curves: Callable[..., RefreshPayload] = fetch_di_curves
    # Retained as a constructor compatibility seam for persisted v1/v2 runs; never dispatched.
    fetch_state_polls: Callable[..., RefreshPayload] | None = None
    refresh_treasury_data: Callable[..., RefreshPayload] = _refresh_treasury_payload
    refresh_sidra_data: Callable[..., RefreshPayload] = refresh_sidra_data
    refresh_derived_snapshots: Callable[[datetime, str], int] = refresh_all_derived_snapshots
    observe_inflation_target_rules: Callable[[], int] | None = None
    refresh_macro_score: Callable[[datetime, str], int] | None = None


@dataclass(frozen=True, slots=True)
class RefreshTask:
    name: str
    provider: str
    hostname: str
    series_expected: int
    run: Callable[[], RefreshPayload]


LEASE_KEY: Final = "daily_refresh"
LEASE_SECONDS: Final = 30 * 60
HEARTBEAT_SECONDS: Final = 60
MAX_GLOBAL_WORKERS: Final = 6
MAX_HOST_WORKERS: Final = 2


def _tasks(force: bool, dependencies: RefreshDependencies) -> tuple[RefreshTask, ...]:
    cache = not force
    sgs_groups = (
        ("sgs_juros", INTEREST_RATES, "01/01/2015"),
        ("sgs_inflacao", INFLATION, "01/01/2015"),
        ("sgs_atividade", ACTIVITY_REFRESH, "01/01/2015"),
        ("sgs_fiscal", FISCAL, "01/01/2001"),
        ("sgs_credito", CREDIT, "01/01/2011"),
        ("sgs_cambio", EXCHANGE, "01/01/2015"),
        ("sgs_setor_externo", SETOR_EXTERNO, "01/01/2015"),
        ("sgs_complementares", COMPLEMENTARY_REFRESH, "01/01/2015"),
        ("sgs_ipca_grupos", IPCA_GROUPS, None),
        ("sgs_ipca_naturezas", IPCA_NATURE, None),
        ("sgs_ipca_nucleos", IPCA_CORE, None),
        ("sgs_ipca_precos", IPCA_PRICES, None),
    )
    sgs_tasks = tuple(
        RefreshTask(
            name,
            "bcb_sgs",
            "api.bcb.gov.br",
            len(codes),
            lambda codes=codes, start=start: dependencies.fetch_sgs_batch(
                codes,
                start_date=start,
                use_cache=cache,
            ),
        )
        for name, codes, start in sgs_groups
    )
    return sgs_tasks + (
        RefreshTask("focus", "focus", "olinda.bcb.gov.br", 5, lambda: dependencies.fetch_all_focus(use_cache=cache)),
        RefreshTask("focus_12m", "focus", "olinda.bcb.gov.br", 1, lambda: dependencies.fetch_focus_12m(use_cache=cache)),
        RefreshTask("anbima", "anbima", "s3-data-prd-use1-precos.s3.us-east-1.amazonaws.com", 1, lambda: dependencies.fetch_anbima_ima(use_cache=cache)),
        RefreshTask("b3_di", "b3", "www.b3.com.br", 1, lambda: dependencies.fetch_di_curves(days=30, use_cache=cache)),
        RefreshTask("tesouro", "tesouro", "www.tesourotransparente.gov.br", 1, dependencies.refresh_treasury_data),
        RefreshTask("ibge_sidra", "ibge_sidra", "apisidra.ibge.gov.br", 5, dependencies.refresh_sidra_data),
    )


def ipca_group_window_tasks(
    force: bool,
    dependencies: RefreshDependencies,
) -> tuple[RefreshTask, ...]:
    """Represent every IPCA group series/window as an independently resumable item."""
    tasks: list[RefreshTask] = []
    for code_name, code in IPCA_GROUPS.items():
        for index, (window_start, window_end) in enumerate(sgs_refresh_windows()):
            window_name = f"sgs_ipca_grupos_{code}_{index}"
            tasks.append(
                RefreshTask(
                    window_name,
                    "bcb_sgs",
                    "api.bcb.gov.br",
                    1,
                    lambda code=code, code_name=code_name, window_start=window_start, window_end=window_end: dependencies.fetch_sgs_batch(
                        {code_name: code},
                        start_date=window_start,
                        end_date=window_end,
                        use_cache=not force,
                        mark_refreshed=False,
                        fail_fast=True,
                        request_timeout=20,
                    ),
                )
            )
    return tuple(tasks)


def sgs_inflation_window_tasks(
    force: bool,
    dependencies: RefreshDependencies,
    as_of: date,
) -> tuple[RefreshTask, ...]:
    """Create independently resumable calendar-year windows for inflation series."""
    tasks: list[RefreshTask] = []
    for series_name, code in INFLATION.items():
        for start_text, end_text in sgs_annual_windows("2015-01-01", as_of.isoformat()):
            start_day, start_month, start_year = (int(part) for part in start_text.split("/"))
            end_day, end_month, end_year = (int(part) for part in end_text.split("/"))
            start = date(start_year, start_month, start_day)
            end = date(end_year, end_month, end_day)
            name = f"sgs_inflacao_w{code}_{start.isoformat()}_{end.isoformat()}"

            def fetch_window(
                *, code: int = code, series_name: str = series_name,
                start_text: str = start_text, end_text: str = end_text,
            ) -> RefreshPayload:
                fetched = dependencies.fetch_sgs_window(
                    code,
                    start_date=start_text,
                    end_date=end_text,
                    # A cache hit is not a new network validation or freshness event.
                    use_cache=False,
                    fail_fast=True,
                    request_timeout=20,
                )
                value = fetched.value if isinstance(fetched, FetchResult) else fetched
                return {series_name: value}

            tasks.append(RefreshTask(name, "bcb_sgs", "api.bcb.gov.br", 1, fetch_window))
    return tuple(tasks)


def sgs_interest_window_tasks(
    force: bool,
    dependencies: RefreshDependencies,
    as_of: date,
) -> tuple[RefreshTask, ...]:
    """Create frozen annual work items for the two large daily interest series."""
    tasks: list[RefreshTask] = []
    for label, code in (("selic_meta", 432), ("tr", 226)):
        for start_text, end_text in sgs_annual_windows("2015-01-01", as_of.isoformat()):
            start_day, start_month, start_year = (int(part) for part in start_text.split("/"))
            end_day, end_month, end_year = (int(part) for part in end_text.split("/"))
            start = date(start_year, start_month, start_day)
            end = date(end_year, end_month, end_day)
            name = f"sgs_juros_w{code}_{start.isoformat()}_{end.isoformat()}"

            def fetch_window(
                *, code: int = code, label: str = label,
                start_text: str = start_text, end_text: str = end_text,
            ) -> RefreshPayload:
                fetched = dependencies.fetch_sgs_window(
                    code,
                    start_date=start_text,
                    end_date=end_text,
                    use_cache=not force,
                    fail_fast=True,
                    request_timeout=20,
                )
                value = fetched.value if isinstance(fetched, FetchResult) else fetched
                return {label: value}

            tasks.append(RefreshTask(name, "bcb_sgs", "api.bcb.gov.br", 1, fetch_window))
    return tuple(tasks)


def sgs_interest_remainder_task(
    force: bool,
    dependencies: RefreshDependencies,
) -> RefreshTask:
    remaining = {name: code for name, code in INTEREST_RATES.items() if code not in {432, 226}}
    source = next(task for task in _tasks(force, dependencies) if task.name == "sgs_juros")

    def fetch_remainder() -> RefreshPayload:
        return dependencies.fetch_sgs_batch(
            remaining,
            start_date="01/01/2015",
            use_cache=not force,
            mark_refreshed=False,
        )

    return replace(source, name="sgs_juros_other", series_expected=len(remaining), run=fetch_remainder)


def _payload_counts(payload: RefreshPayload) -> tuple[int, int, bool]:
    if payload.get("error"):
        return 0, 0, True
    if payload.get("receipt_replay") is True:
        return 1, 0, False
    populated = 0
    records = 0
    has_gap = False
    for key, value in payload.items():
        if isinstance(value, Mapping) and cast(Mapping[str, object], value).get("error"):
            has_gap = True
            committed_count = cast(Mapping[str, object], value).get("committed_count")
            if isinstance(committed_count, int) and not isinstance(committed_count, bool) and committed_count > 0:
                populated += 1
                records += committed_count
            continue
        container_length = _container_length(cast(object, value))
        if isinstance(value, (list, dict)) and value:
            populated += 1
            records += container_length
        elif isinstance(value, (list, dict)):
            has_gap = True
        elif key == "rows" and isinstance(value, int) and not isinstance(value, bool) and value > 0:
            populated += 1
            records += value
    return populated, records, has_gap


def _payload_error_code(payload: RefreshPayload) -> str | None:
    if payload.get("error"):
        return str(payload.get("error_code") or "ProviderReportedError")[:80]
    if any(
        isinstance(value, Mapping) and bool(cast(Mapping[str, object], value).get("error"))
        for value in payload.values()
    ):
        return "PartialSourceFailures"
    return None


def _container_length(value: object) -> int:
    return length_hint(value)


def _error_code(error: BaseException) -> str:
    if isinstance(error, InvalidSGSPayloadError):
        return error.error_code
    if isinstance(error, httpx.HTTPStatusError):
        return f"HTTPStatusError:{error.response.status_code}"
    return type(error).__name__[:80]


def run_refresh_task_detailed(
    task: RefreshTask, *, run_id: str | None = None, cycle_id: str | None = None,
) -> RefreshTaskExecution:
    """Execute one source task under a lease owned by the caller."""
    started_at = datetime.now(timezone.utc)
    record_refresh_task_start(task.name, task.provider, task.series_expected, started_at.isoformat())
    started = perf_counter()
    retry_not_before: str | None = None
    error_category: str | None = None
    payload: RefreshPayload = {}
    context_token = set_refresh_context(run_id, task.name, cycle_id)
    try:
        payload = task.run()
        series_updated, record_count, has_gap = _payload_counts(payload)
        error_code = _payload_error_code(payload)
        if payload.get("receipt_replay") is True:
            status = "ok"
            error_code = None
        elif payload.get("error"):
            status: TaskStatus = "error"
            error_code = error_code or "ProviderReportedError"
            retry_at = payload.get("retry_not_before")
            category_value = payload.get("error_category")
            if isinstance(category_value, str):
                error_category = category_value
        elif series_updated == 0 or record_count == 0:
            status = "error"
            error_code = error_code or "NoCommittedObservations"
        else:
            status = "partial" if has_gap else "ok"
            error_code = error_code if has_gap else None
    except Exception as exc:  # noqa: BLE001 - isolate the provider task boundary.
        committed_count = getattr(exc, "committed_count", None)
        series_updated = int(committed_count > 0) if isinstance(committed_count, int) else 0
        record_count = committed_count if isinstance(committed_count, int) else 0
        status = "error"
        error_code = _error_code(exc)
        retry_at = getattr(exc, "retry_not_before", None)
        if isinstance(retry_at, datetime):
            retry_not_before = retry_at.isoformat()
        category = getattr(exc, "error_category", None)
        error_category = getattr(category, "value", str(category)) if category is not None else None
        if isinstance(exc, httpx.HTTPStatusError) and error_category is None:
            from ingestion_policy import classify_exception

            error_category = classify_exception(exc).value
        if error_code == "RetryBudgetExceeded" and retry_not_before is not None:
            # A prior 429's shared publisher advice survives later request-budget
            # exhaustion; do not lose the deferral behind a secondary error.
            error_code = "RetryDeferred"
    finally:
        reset_refresh_context(context_token)
    finished_at = datetime.now(timezone.utc)
    duration_ms = round((perf_counter() - started) * 1000)
    record_refresh_task_finish(
        task.name,
        status,
        series_updated,
        record_count,
        duration_ms,
        error_code,
        finished_at.isoformat(),
        record_success_timestamp=payload.get("receipt_replay") is not True,
    )
    result: RefreshTaskResult = {"name": task.name, "status": status, "error_code": error_code}
    retry_at = payload.get("retry_not_before")
    if error_code == "RetryBudgetExceeded" and isinstance(retry_at, str):
        error_code = "RetryDeferred"
        result["error_code"] = error_code
    if isinstance(retry_at, str) and error_code in {"CircuitOpen", "RetryDeferred"}:
        result["retry_not_before"] = retry_at
    elif retry_not_before is not None and error_code in {"CircuitOpen", "RetryDeferred"}:
        result["retry_not_before"] = retry_not_before
    if isinstance(error_category, str):
        result["error_category"] = error_category
    return RefreshTaskExecution(
        result,
        series_updated,
        record_count,
        duration_ms,
    )


def run_refresh_task(
    task: RefreshTask, *, run_id: str | None = None, cycle_id: str | None = None,
) -> RefreshTaskResult:
    return run_refresh_task_detailed(task, run_id=run_id, cycle_id=cycle_id).result


def daily_refresh(force: bool = False, dependencies: RefreshDependencies | None = None) -> RefreshResult:
    active_dependencies = dependencies or RefreshDependencies()
    owner = str(uuid4())
    now = datetime.now(timezone.utc)
    expires = now + timedelta(seconds=LEASE_SECONDS)
    if not acquire_refresh_lease(LEASE_KEY, owner, expires.isoformat(), now.isoformat()):
        raise RefreshInProgressError()

    stop_heartbeat = Event()

    def heartbeat() -> None:
        while not stop_heartbeat.wait(HEARTBEAT_SECONDS):
            deadline = datetime.now(timezone.utc) + timedelta(seconds=LEASE_SECONDS)
            if not heartbeat_refresh_lease(LEASE_KEY, owner, deadline.isoformat()):
                return

    heartbeat_thread = Thread(target=heartbeat, daemon=True)
    heartbeat_thread.start()
    host_locks: dict[str, RLock] = {}
    host_counts: dict[str, int] = {}
    host_condition = RLock()

    def execute(task: RefreshTask) -> RefreshTaskResult:
        with host_condition:
            host_locks.setdefault(task.hostname, RLock())
        host_lock = host_locks[task.hostname]
        while True:
            with host_lock:
                count = host_counts.get(task.hostname, 0)
                if count < MAX_HOST_WORKERS:
                    host_counts[task.hostname] = count + 1
                    break
            stop_heartbeat.wait(0.01)
        try:
            return run_refresh_task(task, run_id=owner, cycle_id=owner)
        finally:
            with host_lock:
                host_counts[task.hostname] -= 1

    try:
        results: list[RefreshTaskResult] = []
        with ThreadPoolExecutor(max_workers=MAX_GLOBAL_WORKERS) as pool:
            futures = [pool.submit(execute, task) for task in _tasks(force, active_dependencies)]
            for future in as_completed(futures):
                results.append(future.result())
        results.sort(key=lambda item: item["name"])
        successes = sum(item["status"] != "error" for item in results)
        if successes:
            phase_results, phase_successes = run_derived_refresh_phase(
                owner,
                active_dependencies.observe_inflation_target_rules,
                active_dependencies.refresh_derived_snapshots,
                active_dependencies.refresh_macro_score,
            )
            results.extend(phase_results)
            results.sort(key=lambda item: item["name"])
            successes += phase_successes
        if successes == len(results) and all(item["status"] == "ok" for item in results):
            overall: TaskStatus = "ok"
        elif successes:
            overall = "partial"
        else:
            overall = "error"
        if any(item["name"] != "derived_snapshots" and item["status"] != "error" for item in results):
            set_meta("last_successful_update", datetime.now(timezone.utc).isoformat())
        return {"status": overall, "tasks": results}
    finally:
        stop_heartbeat.set()
        heartbeat_thread.join(timeout=1)
        release_refresh_lease(LEASE_KEY, owner)
