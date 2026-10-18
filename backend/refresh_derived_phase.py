from collections.abc import Callable
from datetime import datetime, timezone
from time import perf_counter

from db.store import record_refresh_task_finish, record_refresh_task_start
from derived.refresh import DerivedRefreshError
from refresh_reporting import RefreshTaskResult, TaskStatus


def _error_code(error: BaseException) -> str:
    return type(error).__name__[:80]


def run_derived_refresh_phase(
    owner: str,
    observe_inflation_target_rules: Callable[[], int] | None,
    refresh_derived_snapshots: Callable[[datetime, str], int],
    refresh_macro_score: Callable[[datetime, str], int] | None = None,
) -> tuple[list[RefreshTaskResult], int]:
    results: list[RefreshTaskResult] = []
    successes = 0
    if observe_inflation_target_rules is not None:
        started_at = datetime.now(timezone.utc)
        record_refresh_task_start("inflation_target_rules", "derived", 11, started_at.isoformat())
        timer = perf_counter()
        try:
            observations = observe_inflation_target_rules()
            status: TaskStatus = "ok"
            error_code = None
        except Exception as exc:  # noqa: BLE001 - phase boundary isolates observation failures.
            observations = 0
            status = "error"
            error_code = _error_code(exc)
        record_refresh_task_finish(
            "inflation_target_rules",
            status,
            observations,
            observations,
            round((perf_counter() - timer) * 1000),
            error_code,
            datetime.now(timezone.utc).isoformat(),
        )
        results.append({"name": "inflation_target_rules", "status": status, "error_code": error_code})
        successes += status != "error"

    derived_started = datetime.now(timezone.utc)
    record_refresh_task_start("derived_snapshots", "derived", 0, derived_started.isoformat())
    timer = perf_counter()
    try:
        snapshot_count = refresh_derived_snapshots(derived_started, owner)
        status = "ok"
        error_code = None
    except DerivedRefreshError as exc:
        snapshot_count = exc.inserted_count
        status = "error"
        error_code = _error_code(exc)
    except Exception as exc:  # noqa: BLE001 - phase boundary isolates derived failures.
        snapshot_count = 0
        status = "error"
        error_code = _error_code(exc)
    record_refresh_task_finish(
        "derived_snapshots",
        status,
        snapshot_count,
        snapshot_count,
        round((perf_counter() - timer) * 1000),
        error_code,
        datetime.now(timezone.utc).isoformat(),
    )
    results.append({"name": "derived_snapshots", "status": status, "error_code": error_code})
    successes += status != "error"

    if refresh_macro_score is not None:
        score_started = datetime.now(timezone.utc)
        record_refresh_task_start("macro_score", "macro_score", 1, score_started.isoformat())
        timer = perf_counter()
        try:
            score_count = refresh_macro_score(derived_started, owner)
            status = "ok"
            error_code = None
        except Exception as exc:  # noqa: BLE001 - phase boundary isolates Macro Score failures.
            score_count = 0
            status = "error"
            error_code = _error_code(exc)
        record_refresh_task_finish(
            "macro_score",
            status,
            score_count,
            score_count,
            round((perf_counter() - timer) * 1000),
            error_code,
            datetime.now(timezone.utc).isoformat(),
        )
        results.append({"name": "macro_score", "status": status, "error_code": error_code})
        successes += status != "error"
    return results, successes
