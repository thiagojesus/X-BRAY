import json
from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone
from typing import Literal, NotRequired, TypeAlias, TypedDict, cast

from pydantic import BaseModel, ConfigDict

from config import INFLATION, IPCA_GROUPS
from datafetchers.bcb_sgs import sgs_annual_windows
from db.refresh_run_models import (
    RefreshRunSnapshot,
    RefreshRunState,
    RefreshWorkItemRecord,
    RefreshWorkItemStatus,
)
from db.refresh_runs import read_latest_run
from db.sgs_refresh_plan import UnsupportedSGSPlanError, plan_digest, read_sgs_plan
from db.store import (
    list_refresh_tasks,
    sgs_plan_status_inventory,
    source_extractions_for_refresh_run,
    treasury_current_cohort_status,
)
from source_contracts import (
    SGS_CONTRACTS,
    SGS_FULL_PERIOD_IDENTITY_KIND,
    SGS_POINT_IDENTITY_KIND,
)

JsonScalar: TypeAlias = str | int | float | bool | None
JsonValue: TypeAlias = JsonScalar | list["JsonValue"] | dict[str, "JsonValue"]
TaskStatus: TypeAlias = Literal["ok", "partial", "error"]
_RETIRED_TASKS = frozenset({"polymarket"})


class RefreshTaskResult(TypedDict):
    name: str
    status: TaskStatus
    error_code: str | None
    retry_not_before: NotRequired[str]
    error_category: NotRequired[str]


@dataclass(frozen=True, slots=True)
class RefreshTaskExecution:
    result: RefreshTaskResult
    series_updated: int
    record_count: int
    duration_ms: int


class RefreshResult(TypedDict):
    status: TaskStatus
    tasks: list[RefreshTaskResult]


class _RefreshTaskRow(BaseModel):
    model_config = ConfigDict(frozen=True, extra="ignore")

    task: str
    provider: str
    last_attempt: str | None
    last_success: str | None
    status: str
    series_expected: str | int | float | bool | None
    series_updated: str | int | float | bool | None
    record_count: str | int | float | bool | None
    duration_ms: str | int | float | bool | None
    error: str | None


def _integer(value: str | float | bool | None) -> int:
    if isinstance(value, (str, int, float)):
        return int(value)
    return 0


def _latest_timestamp(values: list[str | None]) -> str | None:
    timestamps = [
        parsed.replace(tzinfo=timezone.utc) if parsed.tzinfo is None else parsed
        for value in values
        if value
        for parsed in (datetime.fromisoformat(value.replace("Z", "+00:00")),)
    ]
    return max(timestamps).isoformat() if timestamps else None


def _window_duration_ms(snapshot: RefreshRunSnapshot) -> int:
    duration = 0
    for item in snapshot.items:
        if not item.task.startswith("sgs_ipca_grupos_") or not item.claimed_at or not item.finished_at:
            continue
        claimed = datetime.fromisoformat(item.claimed_at.replace("Z", "+00:00"))
        finished = datetime.fromisoformat(item.finished_at.replace("Z", "+00:00"))
        duration += max(0, int((finished - claimed).total_seconds() * 1000))
    return duration


def _ipca_group_task_row(
    snapshot: RefreshRunSnapshot,
    existing: _RefreshTaskRow | None,
) -> dict[str, object]:
    expected = {
        f"sgs_ipca_grupos_{code}_{window}"
        for code in IPCA_GROUPS.values()
        for window in range(4)
    }
    windows = [item for item in snapshot.items if item.task.startswith("sgs_ipca_grupos_")]
    exact_manifest = len(windows) == len(expected) and {item.task for item in windows} == expected
    recognized = [item for item in windows if item.task in expected]
    statuses = [item.status for item in recognized]
    all_succeeded = exact_manifest and all(
        item.status is RefreshWorkItemStatus.SUCCEEDED for item in recognized
    )
    completed_data = [
        item for item in recognized
        if item.status in (RefreshWorkItemStatus.SUCCEEDED, RefreshWorkItemStatus.PARTIAL)
    ]
    has_unfinished = any(
        status in (RefreshWorkItemStatus.PENDING, RefreshWorkItemStatus.CLAIMED)
        for status in statuses
    ) or not exact_manifest
    if all_succeeded:
        status = "ok"
    elif has_unfinished or completed_data or any(
        value is RefreshWorkItemStatus.PARTIAL for value in statuses
    ):
        status = "partial"
    else:
        status = "error"

    successful_series = sum(
        all(
            any(
                item.task == f"sgs_ipca_grupos_{code}_{window}"
                and item.status is RefreshWorkItemStatus.SUCCEEDED
                for item in recognized
            )
            for window in range(4)
        )
        for code in IPCA_GROUPS.values()
    )
    old_last_success = existing.last_success if existing else None
    current_last_success = _latest_timestamp([
        item.finished_at
        for item in completed_data
        if item.series_updated > 0
    ])
    error_code = next(
        (item.error_code for item in recognized if item.error_code is not None),
        None,
    )
    if not exact_manifest:
        error_code = "IncompleteIPCAGroupManifest"

    return {
        "task": "sgs_ipca_grupos",
        "provider": existing.provider if existing else "bcb_sgs",
        "last_attempt": _latest_timestamp(
            [snapshot.run.created_at, *(item.claimed_at for item in recognized)]
        ),
        "last_success": _latest_timestamp([old_last_success, current_last_success]),
        "status": status,
        "series_expected": len(IPCA_GROUPS),
        "series_updated": successful_series,
        "record_count": sum(item.record_count for item in completed_data),
        "duration_ms": _window_duration_ms(snapshot),
        "error": error_code,
    }


def _current_ipca_groups(
    rows: list[_RefreshTaskRow],
    snapshot: RefreshRunSnapshot | None,
) -> list[_RefreshTaskRow]:
    if snapshot is None or snapshot.run.manifest_version not in {"daily-refresh-v2", "daily-refresh-v3", "daily-refresh-v4", "daily-refresh-v5", "daily-refresh-v6"}:
        return rows

    existing = next((row for row in rows if row.task == "sgs_ipca_grupos"), None)
    aggregate = _ipca_group_task_row(snapshot, existing)
    replacement = _RefreshTaskRow.model_validate(aggregate)
    return [
        replacement if row.task == "sgs_ipca_grupos" else row
        for row in rows
    ] if existing else [*rows, replacement]


def _interest_work_items(snapshot: RefreshRunSnapshot) -> tuple[set[str], list[RefreshWorkItemRecord]]:
    cutoff = datetime.fromisoformat(snapshot.run.created_at.replace("Z", "+00:00")).date()
    expected = {"sgs_juros_other"}
    for code in (432, 226):
        for start_text, end_text in sgs_annual_windows("2015-01-01", cutoff.isoformat()):
            start_day, start_month, start_year = (int(part) for part in start_text.split("/"))
            end_day, end_month, end_year = (int(part) for part in end_text.split("/"))
            start = date(start_year, start_month, start_day).isoformat()
            end = date(end_year, end_month, end_day).isoformat()
            expected.add(f"sgs_juros_w{code}_{start}_{end}")
    items = [item for item in snapshot.items if item.task.startswith("sgs_juros_w") or item.task == "sgs_juros_other"]
    return expected, items


def _interest_task_row(
    snapshot: RefreshRunSnapshot,
    existing: _RefreshTaskRow | None,
) -> dict[str, object]:
    expected, items = _interest_work_items(snapshot)
    exact = {item.task for item in items} == expected
    recognized = [item for item in items if item.task in expected]
    all_success = exact and all(item.status is RefreshWorkItemStatus.SUCCEEDED for item in recognized)
    completed = [item for item in recognized if item.status in (RefreshWorkItemStatus.SUCCEEDED, RefreshWorkItemStatus.PARTIAL)]
    unfinished = not exact or any(item.status in (RefreshWorkItemStatus.PENDING, RefreshWorkItemStatus.CLAIMED) for item in recognized)
    if all_success:
        status = "ok"
    elif unfinished or completed or any(item.status is RefreshWorkItemStatus.PARTIAL for item in recognized):
        status = "partial"
    else:
        status = "error"
    success_by_code = {
        code: all(
            any(item.task == name and item.status is RefreshWorkItemStatus.SUCCEEDED for item in recognized)
            for name in expected if name.startswith(f"sgs_juros_w{code}_")
        ) and any(name.startswith(f"sgs_juros_w{code}_") for name in expected)
        for code in (432, 226)
    }
    successful_series = 2 if any(item.task == "sgs_juros_other" and item.status is RefreshWorkItemStatus.SUCCEEDED for item in recognized) else 0
    successful_series += sum(success_by_code.values())
    last_success = _latest_timestamp([
        existing.last_success if existing else None,
        *[item.finished_at for item in completed if item.series_updated > 0],
    ])
    return {
        "task": "sgs_juros",
        "provider": existing.provider if existing else "bcb_sgs",
        "last_attempt": _latest_timestamp([snapshot.run.created_at, *(item.claimed_at for item in recognized)]),
        "last_success": last_success,
        "status": status,
        "series_expected": 4,
        "series_updated": successful_series,
        "record_count": sum(item.record_count for item in completed),
        "duration_ms": sum(
            max(0, int((datetime.fromisoformat(item.finished_at.replace("Z", "+00:00")) - datetime.fromisoformat(item.claimed_at.replace("Z", "+00:00"))).total_seconds() * 1000))
            for item in completed if item.claimed_at and item.finished_at
        ),
        "error": "IncompleteSGSInterestManifest" if not exact else next((item.error_code for item in recognized if item.error_code), None),
    }


def _window_iso_date(value: str) -> str:
    try:
        return date.fromisoformat(value).isoformat()
    except ValueError:
        day, month, year = (int(part) for part in value.split("/"))
        return date(year, month, day).isoformat()


def _inflation_work_items(snapshot: RefreshRunSnapshot) -> tuple[set[str], list[RefreshWorkItemRecord]]:
    cutoff = datetime.fromisoformat(snapshot.run.created_at.replace("Z", "+00:00")).date()
    expected = {
        f"sgs_inflacao_w{code}_{_window_iso_date(start)}_{_window_iso_date(end)}"
        for code in INFLATION.values()
        for start, end in sgs_annual_windows("2015-01-01", cutoff.isoformat())
    }
    items = [item for item in snapshot.items if item.task.startswith("sgs_inflacao_w")]
    return expected, items


def _inflation_task_row(
    snapshot: RefreshRunSnapshot,
    existing: _RefreshTaskRow | None,
) -> dict[str, object]:
    expected, items = _inflation_work_items(snapshot)
    exact = {item.task for item in items} == expected
    recognized = [item for item in items if item.task in expected]
    succeeded = [
        item for item in recognized
        if item.status is RefreshWorkItemStatus.SUCCEEDED
        and item.series_updated > 0 and item.record_count > 0
    ]
    completed = [
        item for item in recognized
        if item.status in (RefreshWorkItemStatus.SUCCEEDED, RefreshWorkItemStatus.PARTIAL)
    ]
    unfinished = not exact or any(
        item.status in (RefreshWorkItemStatus.PENDING, RefreshWorkItemStatus.CLAIMED)
        for item in recognized
    )
    if exact and {item.task for item in succeeded} == expected:
        status = "ok"
    elif unfinished or completed or any(item.status is RefreshWorkItemStatus.PARTIAL for item in recognized):
        status = "partial"
    else:
        status = "error"
    successful_series = sum(
        bool(code_items) and all(
            name in {item.task for item in succeeded}
            for name in code_items
        )
        for code in INFLATION.values()
        for code_items in ({name for name in expected if name.startswith(f"sgs_inflacao_w{code}_")},)
    )
    current_success = _latest_timestamp([
        item.finished_at for item in succeeded if item.record_count > 0
    ])
    return {
        "task": "sgs_inflacao",
        "provider": existing.provider if existing else "bcb_sgs",
        "last_attempt": _latest_timestamp([snapshot.run.created_at, *(item.claimed_at for item in recognized)]),
        "last_success": _latest_timestamp([existing.last_success if existing else None, current_success]),
        "status": status,
        "series_expected": len(INFLATION),
        "series_updated": successful_series,
        "record_count": sum(item.record_count for item in completed),
        "duration_ms": sum(
            max(0, int((datetime.fromisoformat(item.finished_at.replace("Z", "+00:00")) - datetime.fromisoformat(item.claimed_at.replace("Z", "+00:00"))).total_seconds() * 1000))
            for item in completed if item.claimed_at and item.finished_at
        ),
        "error": "IncompleteSGSInflationManifest" if not exact else next(
            (item.error_code for item in recognized if item.error_code), None
        ),
    }


def _current_interest_group(
    rows: list[_RefreshTaskRow],
    snapshot: RefreshRunSnapshot | None,
) -> list[_RefreshTaskRow]:
    if snapshot is None or snapshot.run.manifest_version not in {"daily-refresh-v4", "daily-refresh-v5", "daily-refresh-v6"}:
        return rows
    existing = next((row for row in rows if row.task == "sgs_juros"), None)
    aggregate = _RefreshTaskRow.model_validate(_interest_task_row(snapshot, existing))
    return [aggregate if row.task == "sgs_juros" else row for row in rows] if existing else [*rows, aggregate]


def _current_inflation_group(
    rows: list[_RefreshTaskRow],
    snapshot: RefreshRunSnapshot | None,
) -> list[_RefreshTaskRow]:
    if snapshot is None or snapshot.run.manifest_version not in {"daily-refresh-v5", "daily-refresh-v6"}:
        return rows
    existing = next((row for row in rows if row.task == "sgs_inflacao"), None)
    aggregate = _RefreshTaskRow.model_validate(_inflation_task_row(snapshot, existing))
    return [aggregate if row.task == "sgs_inflacao" else row for row in rows] if existing else [*rows, aggregate]


def _window_identity(task_name: str) -> tuple[str, int, str, str] | None:
    parts = task_name.split("_")
    if len(parts) != 5 or parts[0] != "sgs" or parts[1] not in {"juros", "inflacao"}:
        return None
    if not parts[2].startswith("w"):
        return None
    try:
        code = int(parts[2][1:])
        start = date.fromisoformat(parts[3]).isoformat()
        end = date.fromisoformat(parts[4]).isoformat()
    except ValueError:
        return None
    if end < start:
        return None
    return parts[1], code, start, end


def _network_validated_window_keys(
    snapshot: RefreshRunSnapshot,
    extractions: list[dict[str, object]],
) -> list[tuple[tuple[str, int, str, str], datetime, datetime]]:
    completed = {
        item.task: item
        for item in snapshot.items
        if item.status is RefreshWorkItemStatus.SUCCEEDED
        and type(item.finished_at) is str
        and type(item.series_updated) is int
        and item.series_updated > 0
        and type(item.record_count) is int
        and item.record_count > 0
    }
    validated: list[tuple[tuple[str, int, str, str], datetime, datetime]] = []
    for extraction in extractions:
        task_key = extraction.get("task_key")
        if not isinstance(task_key, str) or task_key not in completed:
            continue
        if (
            type(extraction.get("source")) is not str
            or extraction.get("source") != "bcb_sgs"
            or type(extraction.get("indicator")) is not str
            or extraction.get("status") != "success"
            or type(extraction.get("network_called")) is not int
            or extraction.get("network_called") != 1
            or type(extraction.get("request_count")) is not int
            or cast(int, extraction["request_count"]) < 1
            or type(extraction.get("started_at")) is not str
            or type(extraction.get("finished_at")) is not str
            or any(
                type(extraction.get(field)) is not int
                or cast(int, extraction[field]) <= 0
                for field in ("received_count", "valid_count", "persisted_count")
            )
        ):
            continue
        identity = _window_identity(task_key)
        qualifiers_value = extraction.get("qualifiers_json")
        if identity is None or not isinstance(qualifiers_value, str):
            continue
        try:
            qualifiers = json.loads(qualifiers_value)
            if not isinstance(qualifiers, dict):
                continue
            if type(qualifiers.get("series_code")) is not int:
                continue
            if type(qualifiers.get("start_date")) is not str or type(qualifiers.get("end_date")) is not str:
                continue
            qualifier_identity = (
                qualifiers["series_code"],
                _window_iso_date(qualifiers["start_date"]),
                _window_iso_date(qualifiers["end_date"]),
            )
        except (KeyError, TypeError, ValueError, json.JSONDecodeError):
            continue
        spec = next(
            (
                candidate_contract
                for candidate_contract in SGS_CONTRACTS
                if candidate_contract.contract_id == f"sgs:{identity[1]}"
            ),
            None,
        )
        if spec is None:
            continue
        expected_identity_kind = (
            SGS_FULL_PERIOD_IDENTITY_KIND
            if spec.identity == ("series_code", "period_start", "period_end")
            else SGS_POINT_IDENTITY_KIND
            if spec.identity == ("series_code", "reference_date")
            else ""
        )
        if (
            expected_identity_kind
            and type(extraction.get("indicator")) is str
            and extraction.get("indicator") == str(identity[1])
            and type(qualifiers.get("window_policy")) is str
            and qualifiers.get("window_policy") == "calendar-year-v1"
            and type(qualifiers.get("contract_version")) is str
            and qualifiers.get("contract_version") == spec.schema_version
            and type(qualifiers.get("identity_kind")) is str
            and qualifiers.get("identity_kind") == expected_identity_kind
            and type(qualifiers.get("unit")) is str
            and qualifiers.get("unit") == spec.unit
            and qualifier_identity == (identity[1], identity[2], identity[3])
        ):
            try:
                started_at = datetime.fromisoformat(
                    cast(str, extraction["started_at"]).replace("Z", "+00:00")
                )
                finished_at = datetime.fromisoformat(
                    cast(str, extraction["finished_at"]).replace("Z", "+00:00")
                )
            except ValueError:
                continue
            if started_at.tzinfo is None:
                continue
            if finished_at.tzinfo is None:
                continue
            if finished_at < started_at:
                continue
            validated.append((identity, started_at, finished_at))
    return validated


def _window_interval_is_covered(
    failed: tuple[str, int, str, str],
    recoveries: list[tuple[tuple[str, int, str, str], datetime, datetime]],
    failed_at: datetime,
) -> bool:
    lower = date.fromisoformat(failed[2])
    upper = date.fromisoformat(failed[3])
    intervals = sorted(
        (date.fromisoformat(identity[2]), date.fromisoformat(identity[3]))
        for identity, started_at, finished_at in recoveries
        if identity[:2] == failed[:2]
        and started_at > failed_at
        and finished_at > failed_at
    )
    if not intervals:
        return False

    merged: list[tuple[date, date]] = []
    for start, end in intervals:
        if merged and start <= merged[-1][1] + timedelta(days=1):
            prior_start, prior_end = merged[-1]
            merged[-1] = (prior_start, max(prior_end, end))
        else:
            merged.append((start, end))
    return any(start <= lower and end >= upper for start, end in merged)


def _superseded_window_error_names(
    rows: list[_RefreshTaskRow],
    snapshot: RefreshRunSnapshot | None,
    extractions: list[dict[str, object]],
) -> set[str]:
    if snapshot is None or snapshot.run.manifest_version not in {"daily-refresh-v4", "daily-refresh-v5", "daily-refresh-v6"}:
        return set()
    validated = _network_validated_window_keys(snapshot, extractions)
    superseded: set[str] = set()
    for row in rows:
        identity = _window_identity(row.task)
        if identity is None or row.status not in {"error", "partial"} or type(row.last_attempt) is not str:
            continue
        try:
            failed_at = datetime.fromisoformat(row.last_attempt.replace("Z", "+00:00"))
        except ValueError:
            continue
        if failed_at.tzinfo is None:
            continue
        if _window_interval_is_covered(identity, validated, failed_at):
            superseded.add(row.task)
    return superseded


_V6_GROUP_TASKS = {
    "IPCA_GROUPS": "sgs_ipca_grupos",
    "IPCA_NATURE": "sgs_ipca_naturezas",
    "IPCA_CORE": "sgs_ipca_nucleos",
    "IPCA_PRICES": "sgs_ipca_precos",
    "INTEREST_RATES": "sgs_juros",
    "INFLATION": "sgs_inflacao",
    "ACTIVITY": "sgs_atividade",
    "ACTIVITY_SECTORAL": "sgs_atividade",
    "FISCAL": "sgs_fiscal",
    "CREDIT": "sgs_credito",
    "EXCHANGE": "sgs_cambio",
    "EXTERNAL": "sgs_setor_externo",
    "COMPLEMENTARY": "sgs_complementares",
}


def _parse_optional_timestamp(value: object) -> datetime | None:
    if not isinstance(value, str) or not value:
        return None
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    return parsed if parsed.tzinfo is not None else None


def _v6_verified_capture(unit: dict[str, object]) -> datetime | None:
    payload_hash = unit.get("commit_payload_sha256")
    canonical_hash = unit.get("canonical_value_sha256")
    if (
        not isinstance(unit.get("commit_operation_id"), str)
        or not isinstance(unit.get("commit_run_id"), str)
        or not isinstance(payload_hash, str) or len(payload_hash) != 64
        or any(character not in "0123456789abcdef" for character in payload_hash)
        or not isinstance(canonical_hash, str) or len(canonical_hash) != 64
        or any(character not in "0123456789abcdef" for character in canonical_hash)
        or type(unit.get("commit_observed_count")) is not int
        or int(unit["commit_observed_count"]) <= 0
    ):
        return None
    receipt_fields = (
        unit.get("network_provider"), unit.get("network_route"),
        unit.get("network_contract"), unit.get("network_scope_id"),
        unit.get("network_payload_sha256"), unit.get("network_captured_at"),
        unit.get("network_reference_count"),
    )
    if any(value is not None for value in receipt_fields) and (
            unit.get("network_provider") != "bcb_sgs"
            or unit.get("network_route") != "primary"
            or unit.get("network_contract") != "bcb-sgs-json-series-v1"
            or unit.get("network_scope_id") != "bcb_sgs:primary:primary"
            or unit.get("network_payload_sha256") != payload_hash
            or type(unit.get("network_reference_count")) is not int
            or int(unit["network_reference_count"]) <= 0
            or _parse_optional_timestamp(unit.get("network_captured_at")) is None
        ):
        return None
    return _parse_optional_timestamp(unit.get("data_captured_at"))


def _v6_plan_metadata_error(plan: object) -> str | None:
    if plan is None:
        return "InvalidFrozenSGSPlan"
    units = getattr(plan, "units", ())
    for unit in units:
        expected_identity = (
            SGS_FULL_PERIOD_IDENTITY_KIND if unit.series_code == 226
            else SGS_POINT_IDENTITY_KIND
        )
        if (
            unit.group_name not in _V6_GROUP_TASKS
            or type(unit.freshness_days) is not int or unit.freshness_days < 1
            or unit.policy_version != "sgs-coverage-policy-v1"
            or unit.contract_id != f"sgs:{unit.series_code}"
            or unit.contract_version != "acquisition-contract-v1"
            or unit.identity_kind != expected_identity
            or unit.start_date > unit.end_date
            or unit.role not in {"recent", "archive_backfill", "archive_reconcile"}
        ):
            return "InvalidFrozenSGSPlanUnitMetadata"
    return None


def _v6_unit_data_status(unit: dict[str, object], generated: datetime) -> tuple[str, datetime | None]:
    if not isinstance(unit.get("available_count"), int) or unit["available_count"] <= 0:
        return "unavailable", None
    captured_at = _v6_verified_capture(unit)
    if captured_at is None:
        return "unverified", None
    freshness_days = unit.get("freshness_days")
    if type(freshness_days) is not int or freshness_days < 1:
        return "unverified", captured_at
    deadline = captured_at + timedelta(days=freshness_days)
    return ("stale" if generated > deadline else "ok"), deadline


def _v6_treasury_metadata(
    extractions: list[dict[str, object]],
    generated: datetime,
) -> dict[str, JsonValue]:
    attempts = [row for row in extractions if row.get("source") == "tesouro"]
    latest = attempts[-1] if attempts else {}
    cohort = treasury_current_cohort_status()
    captured_at = _parse_optional_timestamp(cohort.get("captured_at"))
    reference_max = cohort.get("reference_max")
    reference_date: date | None = None
    if isinstance(reference_max, str):
        try:
            reference_date = date.fromisoformat(reference_max)
        except ValueError:
            reference_date = None
    if captured_at is None or reference_date is None:
        data_status = "unverified"
        age_days = None
    else:
        age_days = max(0, (captured_at.date() - reference_date).days)
        data_status = "reference_date_behind_capture" if age_days else "matches_capture_day"
    network_called = latest.get("network_called") == 1
    acquisition_status = (
        "network_observed" if network_called
        else "receipt_replayed_no_network" if latest.get("status") == "cache_hit"
        else "not_observed"
    )
    return {
        "data_status": data_status,
        "acquisition_status": acquisition_status,
        "series_available": int(cohort.get("observation_count") or 0),
        "source_reference_min": cohort.get("reference_min"),
        "source_reference_max": reference_max if isinstance(reference_max, str) else None,
        "last_data_captured_at": captured_at.isoformat() if captured_at else None,
        "reference_age_days_at_capture": age_days,
        "cadence_status": "not_assessed_no_publisher_cadence_contract",
        "network_request_count": latest.get("request_count"),
        "network_bytes_and_hops": "see_safe_extraction_trace",
        "checked_at": generated.isoformat(),
    }


def _v6_group_projection(
    snapshot: RefreshRunSnapshot,
    generated: datetime,
    old_rows: list[dict[str, object]],
) -> tuple[list[dict[str, object]], dict[str, object]]:
    run_id = str(snapshot.run.run_id)
    try:
        plan = read_sgs_plan(run_id)
        inventory = sgs_plan_status_inventory(run_id)
    except (UnsupportedSGSPlanError, ValueError, TypeError, KeyError):
        plan = None
        inventory = []
    integrity_error = None
    if plan is None:
        integrity_error = "InvalidFrozenSGSPlan"
    if plan is None or integrity_error is not None:
        degraded = {
            "plan_status": "degraded", "plan_error": integrity_error or "InvalidFrozenSGSPlan",
            "plan_version": None, "plan_sha256": None,
            "recent": {"expected_units": None, "available_units": None,
                       "network_refreshed_units": None, "unverified_units": None},
            "archive": {"selected_units": None, "verified_units": None,
                        "pending_units": None, "deferred_units": None,
                        "failed_units": None, "backlog_status": "unknown"},
        }
        return [{
            "task": "sgs_plan_integrity", "provider": "bcb_sgs", "status": "partial",
            "error": integrity_error or "InvalidFrozenSGSPlan", "series_expected": 0,
            "series_updated": 0, "record_count": 0, "duration_ms": 0,
            "last_attempt": snapshot.run.created_at, "last_success": None,
            "data_status": "unknown", "acquisition_status": "unknown",
            "series_available": 0, "series_network_refreshed": 0,
        }], degraded

    expected_ids = tuple(unit.unit_id for unit in plan.units)
    if tuple(row.get("unit_id") for row in inventory) != expected_ids:
        error = "IncompleteFrozenSGSPlanUnits"
        return [{
            "task": "sgs_plan_integrity", "provider": "bcb_sgs", "status": "partial",
            "error": error, "series_expected": 0, "series_updated": 0,
            "record_count": 0, "duration_ms": 0, "last_attempt": snapshot.run.created_at,
            "last_success": None, "data_status": "unknown", "acquisition_status": "unknown",
            "series_available": 0, "series_network_refreshed": 0,
        }], {"plan_status": "degraded", "plan_error": error,
             "plan_version": plan.plan_version, "plan_sha256": plan_digest(plan),
             "recent": {"expected_units": None, "available_units": None,
                        "network_refreshed_units": None, "unverified_units": None},
             "archive": {"selected_units": None, "verified_units": None,
                         "pending_units": None, "deferred_units": None,
                         "failed_units": None, "backlog_status": "unknown"}}
    metadata_error = _v6_plan_metadata_error(plan)
    if metadata_error:
        error = metadata_error
        return [{
            "task": "sgs_plan_integrity", "provider": "bcb_sgs", "status": "partial",
            "error": error, "series_expected": 0, "series_updated": 0,
            "record_count": 0, "duration_ms": 0, "last_attempt": snapshot.run.created_at,
            "last_success": None, "data_status": "unknown", "acquisition_status": "unknown",
            "series_available": 0, "series_network_refreshed": 0,
        }], {"plan_status": "degraded", "plan_error": error,
             "plan_version": plan.plan_version, "plan_sha256": plan_digest(plan),
             "recent": {"expected_units": None, "available_units": None,
                        "network_refreshed_units": None, "unverified_units": None},
             "archive": {"selected_units": None, "verified_units": None,
                         "pending_units": None, "deferred_units": None,
                         "failed_units": None, "backlog_status": "unknown"}}
    units_by_id = {unit.unit_id: unit for unit in plan.units}
    for row in inventory:
        unit = units_by_id[str(row["unit_id"])]
        persisted_metadata = (
            row.get("group_name"), row.get("freshness_days"), row.get("policy_version"),
            row.get("contract_version"), row.get("identity_kind"), row.get("unit"),
            row.get("method"), row.get("frequency"), row.get("start_date"), row.get("end_date"),
            row.get("series_code"), row.get("indicator_key"), row.get("role"),
        )
        expected_metadata = (
            unit.group_name, unit.freshness_days, unit.policy_version, unit.contract_version,
            unit.identity_kind, unit.unit, unit.method, unit.frequency,
            unit.start_date.isoformat(), unit.end_date.isoformat(), unit.series_code,
            unit.indicator_key, unit.role,
        )
        if persisted_metadata != expected_metadata:
            return [{
                "task": "sgs_plan_integrity", "provider": "bcb_sgs", "status": "partial",
                "error": "FrozenSGSPlanUnitMismatch", "series_expected": 0,
                "series_updated": 0, "record_count": 0, "duration_ms": 0,
                "last_attempt": snapshot.run.created_at, "last_success": None,
                "data_status": "unknown", "acquisition_status": "unknown",
                "series_available": 0, "series_network_refreshed": 0,
            }], {"plan_status": "degraded", "plan_error": "FrozenSGSPlanUnitMismatch",
                 "plan_version": plan.plan_version, "plan_sha256": plan_digest(plan),
                 "recent": {"expected_units": None, "available_units": None,
                            "network_refreshed_units": None, "unverified_units": None},
                 "archive": {"selected_units": None, "verified_units": None,
                             "pending_units": None, "deferred_units": None,
                             "failed_units": None, "backlog_status": "unknown"}}
    if len({unit.series_code for unit in plan.units if unit.role == "recent"}) != sum(
        unit.role == "recent" for unit in plan.units
    ):
        return [{
            "task": "sgs_plan_integrity", "provider": "bcb_sgs", "status": "partial",
            "error": "DuplicateFrozenSGSSeries", "series_expected": 0,
            "series_updated": 0, "record_count": 0, "duration_ms": 0,
            "last_attempt": snapshot.run.created_at, "last_success": None,
            "data_status": "unknown", "acquisition_status": "unknown",
            "series_available": 0, "series_network_refreshed": 0,
        }], {"plan_status": "degraded", "plan_error": "DuplicateFrozenSGSSeries",
             "plan_version": plan.plan_version, "plan_sha256": plan_digest(plan),
             "recent": {"expected_units": None, "available_units": None,
                        "network_refreshed_units": None, "unverified_units": None},
             "archive": {"selected_units": None, "verified_units": None,
                         "pending_units": None, "deferred_units": None,
                         "failed_units": None, "backlog_status": "unknown"}}

    rows_by_group: dict[str, list[dict[str, object]]] = {}
    by_id = {str(row["unit_id"]): row for row in inventory}
    for unit in plan.units:
        if unit.role != "recent":
            continue
        row = by_id[unit.unit_id]
        rows_by_group.setdefault(unit.group_name, []).append(row)
    group_tasks: list[dict[str, object]] = []
    recent_units = [row for row in inventory if row["role"] == "recent"]
    selected_archives = [row for row in inventory if row["role"] != "recent"]
    data_statuses: dict[str, str] = {}
    recent_available = recent_network = recent_unverified = 0
    group_aux: dict[str, dict[str, object]] = {}
    for task_name in dict.fromkeys(_V6_GROUP_TASKS.values()):
        group_names = [name for name, task in _V6_GROUP_TASKS.items() if task == task_name]
        members = [row for name in group_names for row in rows_by_group.get(name, [])]
        if not members:
            continue
        data_by_member = [(_v6_unit_data_status(row, generated), row) for row in members]
        available = sum(int(row.get("available_count") or 0) > 0 for row in members)
        network_refreshed = sum(
            row.get("commit_run_id") == run_id
            and _v6_verified_capture(row) is not None
            for row in members
        )
        member_statuses = [str(row.get("item_status") or "missing") for row in members]
        deferred = sum(bool(row.get("deferral_reason")) for row in members)
        failures = sum(status in {"failed", "partial"} for status in member_statuses)
        complete_items = all(status == "succeeded" for status in member_statuses)
        data_values = [result[0][0] for result in data_by_member]
        if all(value == "ok" for value in data_values):
            data_status = "ok"
        elif "stale" in data_values:
            data_status = "stale"
        elif available == 0:
            data_status = "unavailable"
        else:
            data_status = "unverified"
        if deferred:
            acquisition_status = "deferred"
        elif failures:
            acquisition_status = "error"
        elif complete_items and network_refreshed == len(members):
            acquisition_status = "network_refreshed"
        elif complete_items:
            acquisition_status = "cached_or_unverified"
        elif any(status == "claimed" for status in member_statuses):
            acquisition_status = "in_progress"
        else:
            acquisition_status = "pending"
        summary_status = (
            "ok" if complete_items and data_status == "ok"
            else "error" if failures and not available
            else "partial"
        )
        captures = [_v6_verified_capture(row) for _result, row in data_by_member]
        known_captures = [capture for capture in captures if capture is not None]
        last_capture = max(known_captures) if known_captures else None
        deadlines = [result[1] for result, _row in data_by_member if result[1] is not None]
        deadline = min(deadlines) if deadlines else None
        reference_dates = [
            date.fromisoformat(str(row["reference_end"]))
            for row in members if isinstance(row.get("reference_end"), str)
            and _v6_verified_capture(row) is not None
        ]
        reference_max = max(reference_dates).isoformat() if reference_dates else None
        errors = [
            str(row.get("item_error_code") or row.get("item_last_error_code"))
            for row in members if row.get("item_error_code") or row.get("item_last_error_code")
        ]
        group_tasks.append({
            "task": task_name, "provider": "bcb_sgs", "status": summary_status,
            "error": errors[0] if errors else None,
            "last_attempt": _latest_timestamp([snapshot.run.created_at,
                                                *(row.get("claimed_at") for row in members
                                                  if isinstance(row.get("claimed_at"), str))]),
            "last_success": last_capture.isoformat() if last_capture else None,
            "series_expected": len(members), "series_updated": network_refreshed,
            "record_count": sum(int(row.get("commit_observed_count") or 0)
                                 for row in members if row.get("commit_run_id") == run_id),
            "duration_ms": sum(
                max(0, int((finished - claimed).total_seconds() * 1000))
                for row in members
                for claimed, finished in [(_parse_optional_timestamp(row.get("claimed_at")),
                                           _parse_optional_timestamp(row.get("finished_at")))]
                if claimed is not None and finished is not None
            ),
            "data_status": data_status, "acquisition_status": acquisition_status,
            "series_available": available, "series_network_refreshed": network_refreshed,
            "last_data_captured_at": last_capture.isoformat() if last_capture else None,
            "data_sla_deadline": deadline.isoformat() if deadline else None,
            "current_reference_max": reference_max,
            "provider_deferred": deferred,
        })
        for name in group_names:
            if rows_by_group.get(name):
                data_statuses[name] = data_status
        group_aux[task_name] = {
            "freshness": "ok" if data_status == "ok" else "stale" if data_status == "stale" else "never",
            "sla_deadline": deadline.isoformat() if deadline else None,
            "data_status": data_status,
            "acquisition_status": acquisition_status,
            "series_available": available,
            "series_network_refreshed": network_refreshed,
            "last_data_captured_at": last_capture.isoformat() if last_capture else None,
            "current_reference_max": reference_max,
            "provider_deferred": deferred,
        }
        recent_available += available
        recent_network += network_refreshed
        recent_unverified += sum(value in {"unverified", "unavailable"} for value in data_values)

    archive_verified = sum(bool(row.get("coverage_verified")) for row in selected_archives)
    archive_deferred = sum(bool(row.get("deferral_reason")) for row in selected_archives)
    archive_failed = sum(str(row.get("item_status")) in {"failed", "partial"} for row in selected_archives)
    archive_unproven_success = sum(
        row.get("item_status") == "succeeded" and not bool(row.get("coverage_verified"))
        for row in selected_archives
    )
    archive_pending = len(selected_archives) - archive_verified
    archive_check_dates = [
        parsed for row in selected_archives
        for parsed in [_parse_optional_timestamp(row.get("archive_checked_at"))]
        if parsed is not None
    ]
    last_archive_check = max(archive_check_dates) if archive_check_dates else None
    oldest_archive_age = (
        max(0, (generated - min(archive_check_dates)).days) if archive_check_dates else None
    )
    selected_archive_details = [
        {
            "unit_id": row.get("unit_id"), "series_code": row.get("series_code"),
            "start_date": row.get("start_date"), "end_date": row.get("end_date"),
            "verification_status": "verified" if row.get("coverage_verified")
            else "deferred" if row.get("deferral_reason")
            else "failed" if row.get("item_status") in {"failed", "partial"}
            else "pending",
            "data_available": int(row.get("available_count") or 0) > 0,
            "error_code": row.get("item_error_code") or row.get("item_last_error_code"),
        }
        for row in selected_archives
    ]
    recent_capture_dates = [
        capture for row in recent_units
        for capture in [_v6_verified_capture(row)] if capture is not None
    ]
    recent_last_capture = max(recent_capture_dates) if recent_capture_dates else None
    legacy_history = [
        {"task": row.get("task"), "status": row.get("status"),
         "last_attempt": row.get("last_attempt"), "last_success": row.get("last_success")}
        for row in old_rows if isinstance(row.get("task"), str)
        and str(row.get("task")).startswith("sgs_")
    ][:200]
    summary: dict[str, object] = {
        "plan_status": "valid", "plan_version": plan.plan_version,
        "plan_sha256": plan_digest(plan),
        "recent": {"expected_units": len(recent_units), "available_units": recent_available,
                   "network_refreshed_units": recent_network,
                   "unverified_units": recent_unverified,
                   "last_data_captured_at": recent_last_capture.isoformat()
                   if recent_last_capture else None,
                   "data_status": ("ok" if all(value == "ok" for value in data_statuses.values())
                                   else "partial")},
        "archive": {"selected_units": len(selected_archives), "verified_units": archive_verified,
                    "pending_units": archive_pending, "deferred_units": archive_deferred,
                    "failed_units": archive_failed,
                    "unproven_successes": archive_unproven_success,
                    "available_units": sum(int(row.get("available_count") or 0) > 0
                                            for row in selected_archives),
                    "last_verified_at": last_archive_check.isoformat() if last_archive_check else None,
                    "oldest_check_age_days": oldest_archive_age,
                    "selected": selected_archive_details,
                    "unselected_backlog": "informational_bounded_rotation",
                    "backlog_status": "selected_scope_attention"
                    if archive_deferred or archive_failed or archive_unproven_success
                    else "bounded_rotation"},
        "provider_deferred_items": sum(
            item.status is RefreshWorkItemStatus.PENDING and bool(item.deferral_reason)
            for item in snapshot.items
        ),
        "legacy_sgs_history": legacy_history,
    }
    return group_tasks, {**summary, "_task_metadata": group_aux}


def refresh_status_payload(now: datetime | None = None) -> dict[str, JsonValue]:
    generated = now or datetime.now(timezone.utc)
    snapshot = read_latest_run()
    source_extractions = (
        source_extractions_for_refresh_run(str(snapshot.run.run_id))
        if snapshot is not None and snapshot.run.manifest_version in {"daily-refresh-v4", "daily-refresh-v5", "daily-refresh-v6"}
        else []
    )
    raw_rows = [
        row for row in list_refresh_tasks()
        if row.get("task") not in _RETIRED_TASKS
    ]
    v6_summary: dict[str, object] | None = None
    v6_task_metadata: dict[str, dict[str, object]] = {}
    if snapshot is not None and snapshot.run.manifest_version == "daily-refresh-v6":
        v6_groups, v6_summary = _v6_group_projection(snapshot, generated, raw_rows)
        raw_rows = [row for row in raw_rows if not str(row.get("task", "")).startswith("sgs_")]
        raw_rows.extend(v6_groups)
        v6_task_metadata = cast(dict[str, dict[str, object]], v6_summary.pop("_task_metadata", {}))
        if v6_summary.get("plan_status") == "valid":
            v6_task_metadata["tesouro"] = _v6_treasury_metadata(source_extractions, generated)
        rows = [_RefreshTaskRow.model_validate(row) for row in raw_rows]
    else:
        rows = _current_inflation_group(
            _current_interest_group(
                _current_ipca_groups(
                    [_RefreshTaskRow.model_validate(row) for row in raw_rows], snapshot
                ),
                snapshot,
            ),
            snapshot,
        )
        superseded = _superseded_window_error_names(rows, snapshot, source_extractions)
        if superseded:
            rows = [row for row in rows if row.task not in superseded]
    tasks: list[JsonValue] = []
    usable = 0
    healthy = 0
    for row in rows:
        raw_success = row.last_success
        if raw_success:
            success_at = datetime.fromisoformat(raw_success.replace("Z", "+00:00"))
            if success_at.tzinfo is None:
                success_at = success_at.replace(tzinfo=timezone.utc)
            deadline = success_at + timedelta(hours=30)
            freshness = "stale" if generated > deadline else "ok"
            usable += 1
            if row.status == "ok" and freshness == "ok":
                healthy += 1
            sla_deadline: JsonValue = deadline.isoformat()
        else:
            freshness = "never"
            sla_deadline = None
        task_payload: dict[str, JsonValue] = {
            "task": row.task,
            "provider": row.provider,
            "last_attempt": row.last_attempt,
            "last_success": raw_success,
            "status": row.status,
            "series_expected": _integer(row.series_expected),
            "series_updated": _integer(row.series_updated),
            "record_count": _integer(row.record_count),
            "duration_ms": _integer(row.duration_ms),
            "freshness": freshness,
            "sla_deadline": sla_deadline,
            "error_code": row.error,
        }
        task_payload.update(cast(dict[str, JsonValue], v6_task_metadata.get(row.task, {})))
        tasks.append(task_payload)
    if not rows:
        overall = "never_run"
    elif healthy == len(rows):
        overall = "ok"
    elif usable:
        overall = "partial"
    else:
        overall = "error"
    if (
        snapshot is not None
        and snapshot.run.manifest_version in {"daily-refresh-v2", "daily-refresh-v3", "daily-refresh-v4", "daily-refresh-v5", "daily-refresh-v6"}
        and snapshot.run.state is not RefreshRunState.SUCCEEDED
        and overall == "ok"
    ):
        overall = "partial"
    if v6_summary is not None and v6_summary.get("plan_status") == "degraded" or v6_summary is not None and overall == "error":
        overall = "partial"
    if v6_summary is not None:
        archive = v6_summary.get("archive")
        if isinstance(archive, dict) and (
            archive.get("deferred_units", 0) or archive.get("failed_units", 0)
            or archive.get("unproven_successes", 0)
        ):
            overall = "partial"
        treasury_metadata = v6_task_metadata.get("tesouro", {})
        if treasury_metadata.get("data_status") == "reference_date_behind_capture":
            overall = "partial"
    response: dict[str, JsonValue] = {
        "generated_at": generated.isoformat(), "overall_status": overall, "tasks": tasks,
    }
    if v6_summary is not None:
        response["sgs_incremental"] = cast(JsonValue, v6_summary)
    return response
