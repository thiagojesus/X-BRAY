"""Pure, frozen SGS incremental planning and receipt-backed archive coverage.

Lookbacks and archive cadence are versioned engineering defaults, not BCB
publication guarantees. This module performs no provider or business-row I/O.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import asdict, dataclass, replace
from datetime import date, datetime, timedelta, timezone
from typing import Final, Literal, cast

from db import store
from db.refresh_run_sql import Connection, execute_many
from indicator_catalog import INDICATOR_CATALOG, CatalogGroup, Frequency, IndicatorSpec
from source_contracts import (
    SGS_CONTRACTS,
    SGS_FULL_PERIOD_IDENTITY_KIND,
    SGS_POINT_IDENTITY_KIND,
)

PLAN_VERSION: Final = "sgs-incremental-plan-v1"
POLICY_VERSION: Final = "sgs-coverage-policy-v1"
MAX_ARCHIVE_UNITS_PER_RUN: Final = 4
ARCHIVE_REVERIFY_DAYS: Final = 365

Role = Literal["recent", "archive_backfill", "archive_reconcile"]


def _row_text(row: dict[str, object], key: str) -> str:
    value = row.get(key)
    if not isinstance(value, str):
        raise UnsupportedSGSPlanError(f"persisted SGS field {key} is not text")
    return value


def _row_int(row: dict[str, object], key: str) -> int:
    value = row.get(key)
    if isinstance(value, bool) or not isinstance(value, int):
        raise UnsupportedSGSPlanError(f"persisted SGS field {key} is not an integer")
    return value


def _row_role(row: dict[str, object], key: str = "role") -> Role:
    value = _row_text(row, key)
    if value == "recent":
        return "recent"
    if value == "archive_backfill":
        return "archive_backfill"
    if value == "archive_reconcile":
        return "archive_reconcile"
    raise UnsupportedSGSPlanError("persisted SGS role is unsupported")


def _row_optional_date(row: dict[str, object], key: str) -> date | None:
    value = row.get(key)
    if value is None:
        return None
    if not isinstance(value, str):
        raise UnsupportedSGSPlanError(f"persisted SGS field {key} is not nullable text")
    return date.fromisoformat(value)


def _canonical_date(value: str) -> str:
    try:
        parsed = date.fromisoformat(value)
    except ValueError:
        try:
            day, month, year = (int(part) for part in value.split("/"))
            parsed = date(year, month, day)
        except (TypeError, ValueError) as exc:
            raise ValueError("SGS date must be ISO or DD/MM/YYYY") from exc
        if parsed.strftime("%d/%m/%Y") != value:
            raise ValueError("SGS date must be ISO or DD/MM/YYYY")
    return parsed.isoformat()

_LOOKBACK: Final[dict[Frequency, tuple[str, int]]] = {
    Frequency.DAILY: ("days", 90),
    Frequency.MARKET: ("days", 90),
    Frequency.MONTHLY: ("months", 24),
    Frequency.MOVING_QUARTER: ("months", 24),
    Frequency.QUARTERLY: ("months", 24),
    Frequency.ANNUAL: ("years", 6),
}
_APPROVED_GROUP_START: Final[dict[CatalogGroup, date]] = {
    CatalogGroup.INTEREST_RATES: date(2015, 1, 1),
    CatalogGroup.INFLATION: date(2015, 1, 1),
    CatalogGroup.ACTIVITY: date(2015, 1, 1),
    CatalogGroup.ACTIVITY_SECTORAL: date(2015, 1, 1),
    CatalogGroup.FISCAL: date(2001, 1, 1),
    CatalogGroup.CREDIT: date(2011, 1, 1),
    CatalogGroup.EXCHANGE: date(2015, 1, 1),
    CatalogGroup.EXTERNAL: date(2015, 1, 1),
    CatalogGroup.COMPLEMENTARY: date(2015, 1, 1),
    CatalogGroup.IPCA_GROUPS: date(1990, 1, 1),
    CatalogGroup.IPCA_NATURE: date(1990, 1, 1),
    CatalogGroup.IPCA_CORE: date(1990, 1, 1),
    CatalogGroup.IPCA_PRICES: date(1990, 1, 1),
}


class UnsupportedSGSPlanError(ValueError):
    """Catalog metadata cannot be planned without inventing scope."""


@dataclass(frozen=True, slots=True)
class SGSPlanUnit:
    unit_id: str
    ordinal: int
    role: Role
    series_code: int
    indicator_key: str
    group_name: str
    freshness_days: int
    policy_version: str
    contract_version: str
    contract_id: str
    identity_kind: str
    unit: str
    method: str
    frequency: str
    start_date: date
    end_date: date


@dataclass(frozen=True, slots=True)
class SGSPlan:
    plan_version: str
    policy_version: str
    as_of: date
    units: tuple[SGSPlanUnit, ...]


@dataclass(frozen=True, slots=True)
class CoverageReceipt:
    operation_id: str
    run_id: str
    unit_id: str
    source: str
    series_code: int
    contract_id: str
    contract_version: str
    identity_kind: str
    scope_role: str
    policy_version: str
    requested_start: date
    requested_end: date
    payload_sha256: str
    observed_count: int
    captured_at: datetime
    reference_start: date | None
    reference_end: date | None
    business_receipt_id: str


@dataclass(frozen=True, slots=True)
class SGSPlanBinding:
    run_id: str
    plan_sha256: str
    unit: SGSPlanUnit


@dataclass(frozen=True, slots=True)
class SGSUnitCommitReceipt:
    """Atomic evidence written only with primary SGS business observations."""

    operation_id: str
    extraction_id: str
    run_id: str
    unit_id: str
    plan_sha256: str
    series_code: int
    contract_id: str
    contract_version: str
    identity_kind: str
    scope_role: str
    policy_version: str
    requested_start: date
    requested_end: date
    payload_sha256: str
    canonical_value_sha256: str
    observed_count: int
    reference_start: date
    reference_end: date
    captured_at: datetime


def canonical_sgs_values(
    series_code: int,
    records: tuple[dict[str, object], ...] | list[dict[str, object]],
) -> tuple[str, int, date, date]:
    """Hash canonical unique SGS identities and report their committed bounds."""
    periods = series_code == 226
    canonical: dict[tuple[str, ...], tuple[object, ...]] = {}
    for record in records:
        start_value = record.get("data")
        value = record.get("valor")
        if not isinstance(start_value, str) or isinstance(value, bool):
            raise TypeError("SGS unit values must contain a date and numeric value")
        start = _canonical_date(start_value)
        numeric = float(str(value).replace(",", "."))
        if not (float("-inf") < numeric < float("inf")):
            raise ValueError("SGS unit values must be finite")
        if periods:
            end_value = record.get("dataFim")
            if not isinstance(end_value, str):
                raise ValueError("TR unit values require a full period end")
            end = _canonical_date(end_value)
            if end <= start:
                raise ValueError("TR unit period must have a later end date")
            identity = (start, end)
            value_row: tuple[object, ...] = (series_code, start, end, numeric)
        else:
            identity = (start,)
            value_row = (series_code, start, numeric)
        previous = canonical.get(identity)
        if previous is not None and previous != value_row:
            raise ValueError("conflicting duplicate SGS unit identity")
        canonical[identity] = value_row
    if not canonical:
        raise ValueError("empty SGS cohort cannot prove unit coverage")
    rows = [canonical[key] for key in sorted(canonical)]
    refs_start = [date.fromisoformat(str(row[1])) for row in rows]
    refs_end = [date.fromisoformat(str(row[2] if periods else row[1])) for row in rows]
    digest = hashlib.sha256(
        json.dumps(rows, sort_keys=True, separators=(",", ":"), allow_nan=False).encode()
    ).hexdigest()
    return digest, len(rows), min(refs_start), max(refs_end)


def _month_shift(value: date, months: int) -> date:
    month_index = value.year * 12 + value.month - 1 - months
    year, month_zero = divmod(month_index, 12)
    month = month_zero + 1
    day = min(value.day, _days_in_month(year, month))
    return date(year, month, day)


def _days_in_month(year: int, month: int) -> int:
    if month == 12:
        return 31
    return (date(year, month + 1, 1) - date(year, month, 1)).days


def _year_shift(value: date, years: int) -> date:
    year = value.year - years
    return date(year, value.month, min(value.day, _days_in_month(year, value.month)))


def _recent_start(spec: IndicatorSpec, as_of: date) -> date:
    try:
        base = _APPROVED_GROUP_START[spec.group]
        lookback_kind, amount = _LOOKBACK[spec.frequency]
    except KeyError as exc:
        raise UnsupportedSGSPlanError(f"unsupported SGS frequency/group: {spec.key}") from exc
    if int(spec.provider_code) == 226:
        lookback = as_of - timedelta(days=90)
    elif lookback_kind == "days":
        lookback = as_of - timedelta(days=amount)
    elif lookback_kind == "months":
        lookback = _month_shift(as_of, amount)
    else:
        lookback = _year_shift(as_of, amount)
    # Runtime task-group starts are authoritative; catalog metadata alone must
    # not narrow approved history (notably fiscal series beginning in 2001).
    return max(base, lookback)


def _active_catalog() -> tuple[IndicatorSpec, ...]:
    contracts = {int(contract.contract_id.split(":")[1]): contract for contract in SGS_CONTRACTS}
    specs: dict[int, IndicatorSpec] = {}
    for spec in INDICATOR_CATALOG.values():
        code = int(spec.provider_code)
        if code not in contracts:
            continue
        prior = specs.get(code)
        if prior is not None and prior.key != spec.key:
            raise UnsupportedSGSPlanError(f"catalog provider code is not unique: {code}")
        specs[code] = spec
    return tuple(specs[code] for code in sorted(specs))


def active_sgs_series_codes() -> tuple[int, ...]:
    return tuple(int(spec.provider_code) for spec in _active_catalog())


def _unit(spec: IndicatorSpec, role: Role, start: date, end: date, ordinal: int) -> SGSPlanUnit:
    code = int(spec.provider_code)
    contract = next((item for item in SGS_CONTRACTS if item.contract_id == f"sgs:{code}"), None)
    if contract is None:
        raise UnsupportedSGSPlanError(f"missing acquisition contract for SGS {code}")
    identity = SGS_FULL_PERIOD_IDENTITY_KIND if code == 226 else SGS_POINT_IDENTITY_KIND
    method = "full_period" if code == 226 else "date_range"
    identifier = f"{code}:{role}:{start.isoformat()}:{end.isoformat()}"
    return SGSPlanUnit(
        identifier, ordinal, role, code, str(spec.key), spec.group.value,
        spec.freshness_days, POLICY_VERSION,
        contract.schema_version, contract.contract_id, identity, contract.unit,
        method, spec.frequency.value, start, end,
    )


def _archive_windows(start: date, end: date) -> tuple[tuple[date, date], ...]:
    windows: list[tuple[date, date]] = []
    cursor = start
    while cursor < end:
        try:
            anniversary = cursor.replace(year=cursor.year + 1)
        except ValueError:
            anniversary = cursor.replace(year=cursor.year + 1, day=28)
        stop = anniversary - timedelta(days=1)
        # Avoid a moving partial tail: it remains covered by the recent scope
        # until a complete, stable archive unit has aged out of that scope.
        if stop >= end:
            break
        windows.append((cursor, stop))
        cursor = stop + timedelta(days=1)
    return tuple(windows)


def _receipt_scopes(receipts: tuple[CoverageReceipt, ...]) -> dict[tuple[int, date, date], CoverageReceipt]:
    result: dict[tuple[int, date, date], CoverageReceipt] = {}
    for receipt in receipts:
        if (
            receipt.source != "bcb_sgs"
            or receipt.contract_version != "acquisition-contract-v1"
            or receipt.policy_version != POLICY_VERSION
            or receipt.scope_role not in {"archive_backfill", "archive_reconcile"}
        ):
            continue
        key = (receipt.series_code, receipt.requested_start, receipt.requested_end)
        prior = result.get(key)
        if prior is None or receipt.captured_at > prior.captured_at:
            result[key] = receipt
    return result


def build_sgs_plan(
    as_of: date,
    *,
    receipts: tuple[CoverageReceipt, ...] = (),
    last_archive_series_code: int | None = None,
    max_archive_units: int = MAX_ARCHIVE_UNITS_PER_RUN,
    carryover_units: tuple[SGSPlanUnit, ...] = (),
) -> SGSPlan:
    """Build deterministic recent-first units; pure and independent of I/O."""
    if max_archive_units < 0 or max_archive_units > MAX_ARCHIVE_UNITS_PER_RUN:
        raise ValueError("max_archive_units must be between 0 and the policy cap")
    specs = _active_catalog()
    receipt_map = _receipt_scopes(receipts)
    recent: list[SGSPlanUnit] = []
    archive_candidates: list[tuple[bool, datetime, int, date, date, IndicatorSpec]] = []
    for spec in specs:
        code = int(spec.provider_code)
        start = _recent_start(spec, as_of)
        recent.append(_unit(spec, "recent", start, as_of, len(recent)))
        archive_end = start
        approved_start = _APPROVED_GROUP_START[spec.group]
        for archive_start, archive_stop in _archive_windows(approved_start, archive_end):
            receipt = receipt_map.get((code, archive_start, archive_stop))
            if receipt is None:
                archive_candidates.append((True, datetime.min.replace(tzinfo=timezone.utc), code,
                                           archive_start, archive_stop, spec))
            else:
                due = receipt.captured_at + timedelta(days=ARCHIVE_REVERIFY_DAYS)
                if due.date() <= as_of:
                    archive_candidates.append((False, due, code, archive_start, archive_stop, spec))
    # New or unverified scopes lead; verified due scopes then use oldest due time.
    archive_candidates.sort(key=lambda item: (
        not item[0], item[1], (item[2] <= (last_archive_series_code or -1)), item[2], item[3],
    ))
    recent = [
        replace(unit, ordinal=index) for index, unit in enumerate(recent)
    ]
    selected: list[tuple[bool, datetime, int, date, date, IndicatorSpec]] = []
    selected_series: set[int] = set()
    by_code = {int(spec.provider_code): spec for spec in specs}
    carried: list[SGSPlanUnit] = []
    for pending in sorted(carryover_units, key=lambda unit: (unit.series_code, unit.start_date)):
        if len(carried) >= max_archive_units:
            break
        if (
            pending.role not in {"archive_backfill", "archive_reconcile"}
            or pending.series_code not in by_code
            or pending.policy_version != POLICY_VERSION
            or pending.contract_version != "acquisition-contract-v1"
            or pending.series_code in selected_series
            or pending.end_date < pending.start_date
        ):
            continue
        selected_series.add(pending.series_code)
        carried.append(pending)
    for candidate in archive_candidates:
        if len(carried) + len(selected) >= max_archive_units:
            break
        if candidate[2] in selected_series:
            continue
        selected.append(candidate)
        selected_series.add(candidate[2])
        if len(selected) >= max_archive_units:
            break
    archives = [
        replace(unit, ordinal=len(recent) + index)
        for index, unit in enumerate(carried)
    ] + [
        _unit(spec, "archive_backfill" if missing else "archive_reconcile", start, end,
              len(recent) + len(carried) + index)
        for index, (missing, _due, _code, start, end, spec) in enumerate(selected)
    ]
    return SGSPlan(PLAN_VERSION, POLICY_VERSION, as_of, (*recent, *archives))


def freeze_sgs_plan(run_id: str, plan: SGSPlan, created_at: datetime | None = None) -> SGSPlan:
    """Persist once per run; later calls return the original immutable snapshot."""
    canonical = json.dumps(_plan_json(plan), sort_keys=True, separators=(",", ":"))
    digest = hashlib.sha256(canonical.encode()).hexdigest()
    now = (created_at or datetime.now(timezone.utc)).astimezone(timezone.utc).isoformat()
    existing = store.query_rows(
        "SELECT plan_json, plan_sha256 FROM sgs_refresh_plans WHERE run_id = ?", (run_id,)
    )
    if existing:
        frozen_json = _row_text(existing[0], "plan_json")
        if hashlib.sha256(frozen_json.encode()).hexdigest() != _row_text(existing[0], "plan_sha256"):
            raise ValueError("frozen SGS plan digest does not match its persisted snapshot")
        return _plan_from_json(json.loads(frozen_json))
    store.execute_transaction([
        (
            ("INSERT INTO sgs_refresh_plans (run_id, plan_version, as_of, plan_json, plan_sha256, created_at) "
            "VALUES (?, ?, ?, ?, ?, ?)"),
            [(run_id, plan.plan_version, plan.as_of.isoformat(), canonical, digest, now)],
        ),
        (
            ("INSERT INTO sgs_refresh_plan_units (run_id, unit_id, ordinal, role, series_code, indicator_key, "
             "group_name, freshness_days, policy_version, contract_version, identity_kind, unit, method, "
             "frequency, start_date, end_date, updated_at) "
             "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)"),
            [(run_id, unit.unit_id, unit.ordinal, unit.role, unit.series_code, unit.indicator_key,
              unit.group_name, unit.freshness_days, unit.policy_version, unit.contract_version,
              unit.identity_kind, unit.unit, unit.method,
              unit.frequency, unit.start_date.isoformat(), unit.end_date.isoformat(), now)
             for unit in plan.units],
        ),
    ])
    return plan


def _plan_json(plan: SGSPlan) -> dict[str, object]:
    return {
        "plan_version": plan.plan_version,
        "policy_version": plan.policy_version,
        "as_of": plan.as_of.isoformat(),
        "units": [{**asdict(unit), "start_date": unit.start_date.isoformat(),
                   "end_date": unit.end_date.isoformat()} for unit in plan.units],
    }


def plan_digest(plan: SGSPlan) -> str:
    canonical = json.dumps(_plan_json(plan), sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(canonical.encode()).hexdigest()


def _json_object(value: object) -> dict[str, object]:
    if not isinstance(value, dict):
        raise UnsupportedSGSPlanError("frozen SGS plan object is malformed")
    untyped_object = cast(dict[object, object], value)
    if not all(isinstance(key, str) for key in untyped_object):
        raise UnsupportedSGSPlanError("frozen SGS plan object is malformed")
    return cast(dict[str, object], value)


def _json_text(value: dict[str, object], key: str) -> str:
    field = value.get(key)
    if not isinstance(field, str):
        raise UnsupportedSGSPlanError(f"frozen SGS plan field {key} is invalid")
    return field


def _json_int(value: dict[str, object], key: str) -> int:
    field = value.get(key)
    if isinstance(field, bool) or not isinstance(field, int):
        raise UnsupportedSGSPlanError(f"frozen SGS plan field {key} is invalid")
    return field


def _json_unit(value: object) -> SGSPlanUnit:
    item = _json_object(value)
    role_text = _json_text(item, "role")
    if role_text == "recent":
        role: Role = "recent"
    elif role_text == "archive_backfill":
        role = "archive_backfill"
    elif role_text == "archive_reconcile":
        role = "archive_reconcile"
    else:
        raise UnsupportedSGSPlanError("frozen SGS plan role is invalid")
    try:
        start = date.fromisoformat(_json_text(item, "start_date"))
        end = date.fromisoformat(_json_text(item, "end_date"))
    except ValueError as exc:
        raise UnsupportedSGSPlanError("frozen SGS plan dates are invalid") from exc
    return SGSPlanUnit(
        unit_id=_json_text(item, "unit_id"),
        ordinal=_json_int(item, "ordinal"),
        role=role,
        series_code=_json_int(item, "series_code"),
        indicator_key=_json_text(item, "indicator_key"),
        group_name=_json_text(item, "group_name"),
        freshness_days=_json_int(item, "freshness_days"),
        policy_version=_json_text(item, "policy_version"),
        contract_version=_json_text(item, "contract_version"),
        contract_id=_json_text(item, "contract_id"),
        identity_kind=_json_text(item, "identity_kind"),
        unit=_json_text(item, "unit"),
        method=_json_text(item, "method"),
        frequency=_json_text(item, "frequency"),
        start_date=start,
        end_date=end,
    )


def _plan_from_json(raw_value: object) -> SGSPlan:
    value = _json_object(raw_value)
    if value.get("plan_version") != PLAN_VERSION or value.get("policy_version") != POLICY_VERSION:
        raise UnsupportedSGSPlanError("unsupported frozen SGS plan or policy version")
    raw_units = value.get("units")
    if not isinstance(raw_units, list):
        raise UnsupportedSGSPlanError("frozen SGS plan units are malformed")
    unit_values = cast(list[object], raw_units)
    units = tuple(_json_unit(item) for item in unit_values)
    try:
        as_of = date.fromisoformat(_json_text(value, "as_of"))
    except ValueError as exc:
        raise UnsupportedSGSPlanError("frozen SGS plan as_of is invalid") from exc
    return SGSPlan(PLAN_VERSION, POLICY_VERSION, as_of, units)


def plan_item_task_name(unit: SGSPlanUnit) -> str:
    return f"sgs_plan_{unit.unit_id.replace(':', '_')}"


def read_sgs_plan(run_id: str) -> SGSPlan | None:
    rows = store.query_rows(
        "SELECT plan_version, plan_json, plan_sha256 FROM sgs_refresh_plans WHERE run_id = ?", (run_id,)
    )
    if not rows:
        return None
    raw = rows[0]["plan_json"]
    if not isinstance(raw, str) or hashlib.sha256(raw.encode()).hexdigest() != rows[0]["plan_sha256"]:
        raise UnsupportedSGSPlanError("frozen SGS plan digest is invalid")
    try:
        plan = _plan_from_json(json.loads(raw))
    except (KeyError, TypeError, ValueError) as exc:
        raise UnsupportedSGSPlanError("frozen SGS plan snapshot is malformed") from exc
    if _row_text(rows[0], "plan_version") != plan.plan_version:
        raise UnsupportedSGSPlanError("frozen SGS plan version metadata does not match snapshot")
    return plan


def sgs_plan_unit_for_task(run_id: str, task_name: str) -> SGSPlanUnit | None:
    plan = read_sgs_plan(run_id)
    if plan is None:
        return None
    return next((unit for unit in plan.units if plan_item_task_name(unit) == task_name), None)


def current_sgs_plan_binding(
    series_code: int, start_date: date, end_date: date
) -> SGSPlanBinding | None:
    """Resolve a planned unit from the active v6 claim, never request qualifiers."""
    from db.refresh_attempt import current_refresh_attempt

    attempt = current_refresh_attempt()
    if attempt is None:
        return None
    run_id = str(attempt.run_id)
    run_rows = store.query_rows(
        "SELECT manifest_version FROM refresh_runs WHERE run_id = ?", (run_id,)
    )
    if not run_rows or run_rows[0].get("manifest_version") != "daily-refresh-v6":
        return None
    item_key = str(attempt.item_key)
    if not item_key.startswith("sgs_plan_"):
        return None
    plan_rows = store.query_rows(
        "SELECT plan_sha256 FROM sgs_refresh_plans WHERE run_id = ?", (run_id,)
    )
    plan = read_sgs_plan(run_id)
    if plan is None or not plan_rows:
        raise UnsupportedSGSPlanError("active v6 refresh claim has no frozen SGS plan")
    unit = next(
        (candidate for candidate in plan.units if plan_item_task_name(candidate) == item_key),
        None,
    )
    if unit is None:
        raise UnsupportedSGSPlanError("active v6 refresh claim is absent from its frozen SGS plan")
    if (
        unit.series_code != series_code
        or unit.start_date != start_date
        or unit.end_date != end_date
        or unit.policy_version != POLICY_VERSION
    ):
        raise UnsupportedSGSPlanError("SGS request scope does not match its frozen plan unit")
    return SGSPlanBinding(run_id, _row_text(plan_rows[0], "plan_sha256"), unit)


def make_sgs_unit_commit_receipt(
    binding: SGSPlanBinding,
    extraction_id: str | None,
    network_receipt: object | None,
    records: list[dict[str, object]],
) -> SGSUnitCommitReceipt:
    """Bind a validated primary payload to the active frozen SGS unit."""
    from source_ingestion import ValidatedNetworkReceipt

    unit = binding.unit
    if not isinstance(network_receipt, ValidatedNetworkReceipt):
        raise TypeError("SGS unit commit requires a validated network receipt")
    if (
        not extraction_id
        or network_receipt.provider != "bcb_sgs"
        or network_receipt.route != "primary"
        or network_receipt.contract != "bcb-sgs-json-series-v1"
        or network_receipt.scope_id != "bcb_sgs:primary:primary"
        or network_receipt.identity != f"series:{unit.series_code}"
        or unit.contract_id != f"sgs:{unit.series_code}"
        or unit.contract_version != "acquisition-contract-v1"
    ):
        raise ValueError("network receipt does not identify this primary SGS unit")
    expected_coverage = f"{unit.start_date.isoformat()}..{unit.end_date.isoformat()}"
    try:
        coverage_parts = network_receipt.coverage.split("..")
        normalized_coverage = "..".join(_canonical_date(part) for part in coverage_parts)
        captured_at = datetime.fromisoformat(network_receipt.captured_at.replace("Z", "+00:00"))
    except (TypeError, ValueError) as exc:
        raise ValueError("validated network receipt has malformed SGS scope or timestamp") from exc
    payload_hash = network_receipt.payload_sha256
    if (
        normalized_coverage != expected_coverage
        or captured_at.tzinfo is None
        or len(payload_hash) != 64
        or any(character not in "0123456789abcdef" for character in payload_hash)
    ):
        raise ValueError("validated network receipt does not match the frozen SGS request")
    value_hash, count, reference_start, reference_end = canonical_sgs_values(
        unit.series_code, records
    )
    return SGSUnitCommitReceipt(
        operation_id=extraction_id,
        extraction_id=extraction_id,
        run_id=binding.run_id,
        unit_id=unit.unit_id,
        plan_sha256=binding.plan_sha256,
        series_code=unit.series_code,
        contract_id=unit.contract_id,
        contract_version=unit.contract_version,
        identity_kind=unit.identity_kind,
        scope_role=unit.role,
        policy_version=unit.policy_version,
        requested_start=unit.start_date,
        requested_end=unit.end_date,
        payload_sha256=payload_hash,
        canonical_value_sha256=value_hash,
        observed_count=count,
        reference_start=reference_start,
        reference_end=reference_end,
        captured_at=captured_at.astimezone(timezone.utc),
    )


def _unit_commit_matches_binding(
    row: dict[str, object], binding: SGSPlanBinding
) -> bool:
    unit = binding.unit
    return (
        _row_text(row, "plan_sha256") == binding.plan_sha256
        and _row_text(row, "run_id") == binding.run_id
        and _row_text(row, "unit_id") == unit.unit_id
        and _row_int(row, "series_code") == unit.series_code
        and _row_text(row, "contract_id") == unit.contract_id
        and _row_text(row, "contract_version") == unit.contract_version
        and _row_text(row, "identity_kind") == unit.identity_kind
        and _row_text(row, "scope_role") == unit.role
        and _row_text(row, "policy_version") == unit.policy_version
        and _row_text(row, "requested_start") == unit.start_date.isoformat()
        and _row_text(row, "requested_end") == unit.end_date.isoformat()
        and _row_text(row, "operation_id") == _row_text(row, "extraction_id")
        and _row_int(row, "observed_count") > 0
    )


def reconcile_sgs_unit_commit(binding: SGSPlanBinding) -> SGSUnitCommitReceipt | None:
    """Promote a prior atomic business receipt without another network attempt."""
    rows = store.query_rows(
        "SELECT * FROM sgs_unit_commits WHERE run_id = ? AND unit_id = ? "
        "ORDER BY captured_at DESC LIMIT 1",
        (binding.run_id, binding.unit.unit_id),
    )
    if not rows:
        return None
    row = rows[0]
    if not _unit_commit_matches_binding(row, binding):
        raise ValueError("durable SGS unit commit conflicts with its frozen plan")
    payload_hash = _row_text(row, "payload_sha256")
    canonical_hash = _row_text(row, "canonical_value_sha256")
    if any(
        len(value) != 64 or any(char not in "0123456789abcdef" for char in value)
        for value in (payload_hash, canonical_hash)
    ):
        raise ValueError("durable SGS unit commit has an invalid payload digest")
    try:
        captured_at = datetime.fromisoformat(_row_text(row, "captured_at"))
        reference_start = date.fromisoformat(_row_text(row, "reference_start"))
        reference_end = date.fromisoformat(_row_text(row, "reference_end"))
    except ValueError as exc:
        raise ValueError("durable SGS unit commit has malformed capture metadata") from exc
    if captured_at.tzinfo is None:
        raise ValueError("durable SGS unit commit timestamp must be timezone-aware")
    receipt = SGSUnitCommitReceipt(
        operation_id=_row_text(row, "operation_id"),
        extraction_id=_row_text(row, "extraction_id"),
        run_id=_row_text(row, "run_id"),
        unit_id=_row_text(row, "unit_id"),
        plan_sha256=_row_text(row, "plan_sha256"),
        series_code=_row_int(row, "series_code"),
        contract_id=_row_text(row, "contract_id"),
        contract_version=_row_text(row, "contract_version"),
        identity_kind=_row_text(row, "identity_kind"),
        scope_role=_row_role(row, "scope_role"),
        policy_version=_row_text(row, "policy_version"),
        requested_start=date.fromisoformat(_row_text(row, "requested_start")),
        requested_end=date.fromisoformat(_row_text(row, "requested_end")),
        payload_sha256=payload_hash,
        canonical_value_sha256=canonical_hash,
        observed_count=_row_int(row, "observed_count"),
        reference_start=reference_start,
        reference_end=reference_end,
        captured_at=captured_at.astimezone(timezone.utc),
    )
    if receipt.scope_role != "recent":
        record_verified_coverage(_coverage_from_unit_commit(receipt))
    return receipt


def _coverage_from_unit_commit(receipt: SGSUnitCommitReceipt) -> CoverageReceipt:
    return CoverageReceipt(
        operation_id=receipt.operation_id,
        run_id=receipt.run_id,
        unit_id=receipt.unit_id,
        source="bcb_sgs",
        series_code=receipt.series_code,
        contract_id=receipt.contract_id,
        contract_version=receipt.contract_version,
        identity_kind=receipt.identity_kind,
        scope_role=receipt.scope_role,
        policy_version=receipt.policy_version,
        requested_start=receipt.requested_start,
        requested_end=receipt.requested_end,
        payload_sha256=receipt.payload_sha256,
        observed_count=receipt.observed_count,
        captured_at=receipt.captured_at,
        reference_start=receipt.reference_start,
        reference_end=receipt.reference_end,
        business_receipt_id=receipt.extraction_id,
    )


def insert_sgs_plan_rows(
    connection: Connection, run_id: str, plan: SGSPlan, created_at: str
) -> None:
    """Insert the plan and units using the refresh-run transaction connection."""
    from db.refresh_run_sql import execute

    canonical = json.dumps(_plan_json(plan), sort_keys=True, separators=(",", ":"))
    digest = hashlib.sha256(canonical.encode()).hexdigest()
    execute(
        connection,
        "INSERT INTO sgs_refresh_plans (run_id, plan_version, as_of, plan_json, plan_sha256, created_at) "
        "VALUES (?, ?, ?, ?, ?, ?)",
        (run_id, plan.plan_version, plan.as_of.isoformat(), canonical, digest, created_at),
    )
    execute_many(
        connection,
        "INSERT INTO sgs_refresh_plan_units (run_id, unit_id, ordinal, role, series_code, indicator_key, "
        "group_name, freshness_days, policy_version, contract_version, identity_kind, unit, method, "
        "frequency, start_date, end_date, updated_at) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
        tuple(
            (run_id, unit.unit_id, unit.ordinal, unit.role, unit.series_code, unit.indicator_key,
             unit.group_name, unit.freshness_days, unit.policy_version, unit.contract_version,
             unit.identity_kind, unit.unit, unit.method,
             unit.frequency, unit.start_date.isoformat(), unit.end_date.isoformat(), created_at)
            for unit in plan.units
        ),
    )


def record_verified_coverage(receipt: CoverageReceipt) -> bool:
    """Persist only proof tied to a known business commit; conflicting IDs fail."""
    if (
        receipt.source != "bcb_sgs"
        or receipt.scope_role not in {"archive_backfill", "archive_reconcile"}
        or receipt.observed_count <= 0
        or not receipt.operation_id
        or not receipt.run_id
        or not receipt.unit_id
        or not receipt.business_receipt_id
        or len(receipt.payload_sha256) != 64
        or any(character not in "0123456789abcdef" for character in receipt.payload_sha256.lower())
        or receipt.requested_end < receipt.requested_start
        or receipt.captured_at.tzinfo is None
        or receipt.reference_start is None
        or receipt.reference_end is None
    ):
        raise ValueError("incomplete or invalid SGS coverage proof")
    known_business = store.query_rows(
        "SELECT * FROM sgs_unit_commits WHERE operation_id = ? AND extraction_id = ? "
        "AND run_id = ? AND unit_id = ? AND series_code = ? AND contract_id = ? "
        "AND contract_version = ? AND identity_kind = ? AND scope_role = ? AND policy_version = ? "
        "AND requested_start = ? AND requested_end = ? AND payload_sha256 = ? AND observed_count = ? "
        "AND reference_start = ? AND reference_end = ? AND captured_at = ?",
        (receipt.operation_id, receipt.business_receipt_id, receipt.run_id, receipt.unit_id,
         receipt.series_code, receipt.contract_id, receipt.contract_version, receipt.identity_kind,
         receipt.scope_role, receipt.policy_version, receipt.requested_start.isoformat(),
          receipt.requested_end.isoformat(), receipt.payload_sha256, receipt.observed_count,
          receipt.reference_start.isoformat(), receipt.reference_end.isoformat(),
          receipt.captured_at.astimezone(timezone.utc).isoformat()),
    )
    if not known_business:
        raise ValueError("coverage requires an exact durable SGS unit-commit receipt")
    canonical_hash = _row_text(known_business[0], "canonical_value_sha256")
    if len(canonical_hash) != 64 or any(char not in "0123456789abcdef" for char in canonical_hash):
        raise ValueError("durable SGS unit commit has an invalid canonical value digest")
    frozen_plan = store.query_rows(
        "SELECT plan_sha256 FROM sgs_refresh_plans WHERE run_id = ?", (receipt.run_id,)
    )
    if not frozen_plan or _row_text(frozen_plan[0], "plan_sha256") != _row_text(
        known_business[0], "plan_sha256"
    ):
        raise ValueError("business commit does not match the frozen SGS plan")
    frozen_units = store.query_rows(
        "SELECT role, series_code, contract_version, identity_kind, policy_version, start_date, end_date "
        "FROM sgs_refresh_plan_units WHERE run_id = ? AND unit_id = ?",
        (receipt.run_id, receipt.unit_id),
    )
    if not frozen_units:
        raise ValueError("coverage proof must match a frozen planned unit")
    unit = frozen_units[0]
    expected = (
        receipt.scope_role, receipt.series_code, receipt.contract_version, receipt.identity_kind,
        receipt.policy_version, receipt.requested_start.isoformat(), receipt.requested_end.isoformat(),
    )
    actual = (
        unit["role"], unit["series_code"], unit["contract_version"], unit["identity_kind"],
        unit["policy_version"], unit["start_date"], unit["end_date"],
    )
    if actual != expected or receipt.scope_role == "recent":
        raise ValueError("coverage proof does not match its frozen archive unit")
    expected_identity = SGS_FULL_PERIOD_IDENTITY_KIND if receipt.series_code == 226 else SGS_POINT_IDENTITY_KIND
    if receipt.contract_id != f"sgs:{receipt.series_code}" or receipt.identity_kind != expected_identity:
        raise ValueError("coverage proof uses an unrecognized SGS contract identity")
    values = (
        receipt.run_id, receipt.unit_id, receipt.source, receipt.series_code,
        receipt.contract_id, receipt.contract_version, receipt.identity_kind, receipt.scope_role,
        receipt.policy_version, receipt.requested_start.isoformat(), receipt.requested_end.isoformat(),
        receipt.payload_sha256, receipt.observed_count, receipt.captured_at.astimezone(timezone.utc).isoformat(),
        receipt.reference_start.isoformat(),
        receipt.reference_end.isoformat(),
        receipt.business_receipt_id, datetime.now(timezone.utc).isoformat(),
    )
    existing = store.query_rows(
        "SELECT * FROM sgs_coverage_receipts WHERE operation_id = ?",
        (receipt.operation_id,),
    )
    if existing:
        prior = existing[0]
        same_receipt = (
            prior["payload_sha256"] == receipt.payload_sha256
            and prior["business_receipt_id"] == receipt.business_receipt_id
            and prior["run_id"] == receipt.run_id
            and prior["unit_id"] == receipt.unit_id
            and prior["source"] == receipt.source
            and prior["series_code"] == receipt.series_code
            and prior["contract_id"] == receipt.contract_id
            and prior["contract_version"] == receipt.contract_version
            and prior["identity_kind"] == receipt.identity_kind
            and prior["scope_role"] == receipt.scope_role
            and prior["policy_version"] == receipt.policy_version
            and prior["requested_start"] == receipt.requested_start.isoformat()
            and prior["requested_end"] == receipt.requested_end.isoformat()
            and prior["observed_count"] == receipt.observed_count
            and prior["reference_start"] == receipt.reference_start.isoformat()
            and prior["reference_end"] == receipt.reference_end.isoformat()
            and prior["captured_at"] == receipt.captured_at.astimezone(timezone.utc).isoformat()
            and prior["reference_start"] == (
                receipt.reference_start.isoformat() if receipt.reference_start else None
            )
            and prior["reference_end"] == (
                receipt.reference_end.isoformat() if receipt.reference_end else None
            )
        )
        if not same_receipt:
            raise ValueError("coverage operation identity conflicts with persisted proof")
        return False
    committed = datetime.now(timezone.utc).isoformat()
    store.execute_transaction([
        (
            ("INSERT INTO sgs_coverage_receipts (operation_id, run_id, unit_id, source, series_code, contract_id, "
            "contract_version, identity_kind, scope_role, policy_version, requested_start, requested_end, "
            "payload_sha256, observed_count, captured_at, reference_start, reference_end, business_receipt_id, committed_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)"),
            [(receipt.operation_id, *values)],
        ),
        (
            ("UPDATE sgs_refresh_plan_units SET status = 'verified', updated_at = ? "
            "WHERE run_id = ? AND unit_id = ?"),
            [(committed, receipt.run_id, receipt.unit_id)],
        ),
        (
            ("INSERT INTO sgs_archive_cursors (source, last_series_code, updated_at) VALUES ('bcb_sgs', ?, ?) "
            "ON CONFLICT(source) DO UPDATE SET last_series_code = excluded.last_series_code, updated_at = excluded.updated_at"),
            [(receipt.series_code, committed)],
        ),
    ])
    return True


def verified_coverage(series_code: int) -> tuple[CoverageReceipt, ...]:
    """Return recognized, policy-current receipt scopes; rows/dates are not evidence."""
    return verified_coverage_for_codes((series_code,)).get(series_code, ())


def verified_coverage_for_codes(
    series_codes: tuple[int, ...],
) -> dict[int, tuple[CoverageReceipt, ...]]:
    """Load the bounded receipt inventory in one parameterized query."""
    if not series_codes:
        return {}
    placeholders = ", ".join("?" for _ in series_codes)
    rows = store.query_rows(
        "SELECT * FROM sgs_coverage_receipts WHERE series_code IN (" + placeholders + ") "
        "AND source = 'bcb_sgs' "
        "AND contract_version = 'acquisition-contract-v1' AND policy_version = ? "
        "ORDER BY requested_start, requested_end",
        (*series_codes, POLICY_VERSION),
    )
    by_code: dict[int, list[CoverageReceipt]] = {code: [] for code in series_codes}
    for row in rows:
        receipt = CoverageReceipt(
            operation_id=_row_text(row, "operation_id"), run_id=_row_text(row, "run_id"),
            unit_id=_row_text(row, "unit_id"), source=_row_text(row, "source"),
            series_code=_row_int(row, "series_code"), contract_id=_row_text(row, "contract_id"),
            contract_version=_row_text(row, "contract_version"),
            identity_kind=_row_text(row, "identity_kind"), scope_role=_row_text(row, "scope_role"),
            policy_version=_row_text(row, "policy_version"),
            requested_start=date.fromisoformat(_row_text(row, "requested_start")),
            requested_end=date.fromisoformat(_row_text(row, "requested_end")),
            payload_sha256=_row_text(row, "payload_sha256"),
            observed_count=_row_int(row, "observed_count"),
            captured_at=datetime.fromisoformat(_row_text(row, "captured_at")),
            reference_start=_row_optional_date(row, "reference_start"),
            reference_end=_row_optional_date(row, "reference_end"),
            business_receipt_id=_row_text(row, "business_receipt_id"),
        )
        by_code[receipt.series_code].append(receipt)
    return {code: tuple(receipts) for code, receipts in by_code.items()}


def pending_archive_units(limit: int = MAX_ARCHIVE_UNITS_PER_RUN) -> tuple[SGSPlanUnit, ...]:
    """Read canonical historical intent from prior frozen plans for carry-forward."""
    if limit < 0:
        raise ValueError("limit cannot be negative")
    rows = store.query_rows(
        "SELECT u.* FROM sgs_refresh_plan_units u LEFT JOIN sgs_coverage_receipts c "
        "ON c.run_id = u.run_id AND c.unit_id = u.unit_id "
        "WHERE u.role IN ('archive_backfill', 'archive_reconcile') "
        "AND u.status IN ('reserved', 'deferred', 'failed') AND c.operation_id IS NULL "
        "ORDER BY u.start_date, u.series_code, u.run_id LIMIT ?",
        (limit,),
    )
    return tuple(
        SGSPlanUnit(
            unit_id=_row_text(row, "unit_id"), ordinal=_row_int(row, "ordinal"),
            role=_row_role(row), series_code=_row_int(row, "series_code"),
            indicator_key=_row_text(row, "indicator_key"),
            group_name=_row_text(row, "group_name"),
            freshness_days=_row_int(row, "freshness_days"),
            policy_version=_row_text(row, "policy_version"),
            contract_version=_row_text(row, "contract_version"),
            contract_id=f"sgs:{_row_int(row, 'series_code')}",
            identity_kind=_row_text(row, "identity_kind"), unit=_row_text(row, "unit"),
            method=_row_text(row, "method"), frequency=_row_text(row, "frequency"),
            start_date=date.fromisoformat(_row_text(row, "start_date")),
            end_date=date.fromisoformat(_row_text(row, "end_date")),
        )
        for row in rows
    )


def set_archive_unit_state(run_id: str, unit_id: str, state: Literal["deferred", "failed"], error_code: str | None = None) -> bool:
    """Keep canonical historical intent pending without advancing coverage."""
    changed = store.query_rows(
        "SELECT 1 AS present FROM sgs_refresh_plan_units WHERE run_id = ? AND unit_id = ? "
        "AND role IN ('archive_backfill', 'archive_reconcile') AND status <> 'verified'",
        (run_id, unit_id),
    )
    if not changed:
        return False
    store.execute_statement(
        "UPDATE sgs_refresh_plan_units SET status = ?, last_error_code = ?, updated_at = ? "
        "WHERE run_id = ? AND unit_id = ? AND status <> 'verified'",
        (state, error_code, datetime.now(timezone.utc).isoformat(), run_id, unit_id),
    )
    return True


def build_next_sgs_plan(as_of: date, max_archive_units: int = MAX_ARCHIVE_UNITS_PER_RUN) -> SGSPlan:
    """Plan using durable verified receipts, fairness cursor, and pending intent."""
    rows = store.query_rows("SELECT last_series_code FROM sgs_archive_cursors WHERE source = 'bcb_sgs'")
    cursor = _row_int(rows[0], "last_series_code") if rows else None
    receipts = verified_coverage_for_codes(active_sgs_series_codes())
    return build_sgs_plan(
        as_of,
        receipts=tuple(receipt for code_receipts in receipts.values() for receipt in code_receipts),
        last_archive_series_code=cursor,
        max_archive_units=max_archive_units,
        carryover_units=pending_archive_units(max_archive_units),
    )
