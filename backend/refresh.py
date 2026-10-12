from collections.abc import Callable, Mapping
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from operator import length_hint
from threading import Event, RLock, Thread
from time import perf_counter
from typing import Final, TypeAlias
from uuid import uuid4

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
from datafetchers.bcb_sgs import fetch_sgs_batch
from datafetchers.focus import fetch_all_focus, fetch_focus_12m
from datafetchers.ibge_sidra import refresh_sidra_data
from datafetchers.polymarket import fetch_state_polls
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
    RefreshTaskResult,
    TaskStatus,
    refresh_status_payload,
)

__all__ = ("RefreshResult", "RefreshTaskResult", "TaskStatus", "refresh_status_payload")

RefreshPayload: TypeAlias = Mapping[str, object]


@dataclass(frozen=True, slots=True)
class RefreshInProgressError(RuntimeError):
    retry_after: int = 60


@dataclass(frozen=True, slots=True)
class RefreshDependencies:
    fetch_sgs_batch: Callable[..., RefreshPayload] = fetch_sgs_batch
    fetch_all_focus: Callable[..., RefreshPayload] = fetch_all_focus
    fetch_focus_12m: Callable[..., RefreshPayload] = fetch_focus_12m
    fetch_anbima_ima: Callable[..., RefreshPayload] = fetch_anbima_ima
    fetch_di_curves: Callable[..., RefreshPayload] = fetch_di_curves
    fetch_state_polls: Callable[..., RefreshPayload] = fetch_state_polls
    refresh_treasury_data: Callable[..., RefreshPayload] = refresh_treasury_data
    refresh_sidra_data: Callable[..., RefreshPayload] = refresh_sidra_data
    refresh_derived_snapshots: Callable[[datetime, str], int] = refresh_all_derived_snapshots
    observe_inflation_target_rules: Callable[[], int] | None = None


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
        RefreshTask("polymarket", "polymarket", "gamma-api.polymarket.com", 1, lambda: dependencies.fetch_state_polls(use_cache=cache)),
        RefreshTask("tesouro", "tesouro", "www.tesourotransparente.gov.br", 1, dependencies.refresh_treasury_data),
        RefreshTask("ibge_sidra", "ibge_sidra", "apisidra.ibge.gov.br", 5, dependencies.refresh_sidra_data),
    )


def _payload_counts(payload: RefreshPayload) -> tuple[int, int, bool]:
    if payload.get("error"):
        return 0, 0, True
    populated = 0
    records = 0
    has_gap = False
    for key, value in payload.items():
        container_length = _container_length(value)
        if isinstance(value, (list, dict)) and value:
            populated += 1
            records += container_length
        elif isinstance(value, (list, dict)):
            has_gap = True
        elif key == "rows" and isinstance(value, int) and not isinstance(value, bool) and value > 0:
            populated += 1
            records += value
    return populated, records, has_gap


def _container_length(value: object) -> int:
    return length_hint(value)


def _error_code(error: BaseException) -> str:
    return type(error).__name__[:80]


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
        started_at = datetime.now(timezone.utc)
        record_refresh_task_start(task.name, task.provider, task.series_expected, started_at.isoformat())
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
        started = perf_counter()
        try:
            payload = task.run()
            series_updated, record_count, has_gap = _payload_counts(payload)
            if payload.get("error"):
                status = "error"
                error_code = "ProviderReportedError"
            elif series_updated == 0 or record_count == 0:
                status = "error"
                error_code = "NoCommittedObservations"
            else:
                status = "partial" if has_gap else "ok"
                error_code = None
        except Exception as exc:  # noqa: BLE001 - task boundary isolates provider failures.
            series_updated = 0
            record_count = 0
            status = "error"
            error_code = _error_code(exc)
        finally:
            with host_lock:
                host_counts[task.hostname] -= 1
        finished_at = datetime.now(timezone.utc)
        record_refresh_task_finish(
            task.name,
            status,
            series_updated,
            record_count,
            round((perf_counter() - started) * 1000),
            error_code,
            finished_at.isoformat(),
        )
        return {"name": task.name, "status": status, "error_code": error_code}

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
