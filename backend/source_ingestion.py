"""Common audited boundary for all source refresh operations.

Provider modules retain their parsers and persistence rules. Their public refresh
functions enter through a typed adapter here, so cache returns, network fetches,
validation and persistence outcomes are recorded consistently.
"""

from __future__ import annotations

import inspect
import json
import logging
from abc import ABC, abstractmethod
from collections.abc import Callable, Generator
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone
from functools import wraps
from typing import TypeVar, cast
from uuid import uuid4

from db.refresh_attempt import current_refresh_attempt, record_refresh_attempt_stage
from db.store import (
    claim_source_access_probe,
    finish_source_extraction,
    record_source_access_failure,
    record_source_access_success,
    release_source_access_probe,
    start_source_extraction,
)
from ingestion_policy import (
    CircuitOpen,
    ErrorCategory,
    RetryBudget,
    RetryProfile,
    classify_exception,
    current_retry_budget,
    install_retry_budget,
    reset_retry_budget,
    retry_profile,
)

RefreshContext = tuple[str | None, str | None, str | None]
T = TypeVar("T")
_refresh_context: ContextVar[RefreshContext] = ContextVar(
    "source_refresh_context", default=(None, None, None)
)
_active_extraction_id: ContextVar[str | None] = ContextVar(
    "active_source_extraction_id", default=None
)
_requested_extraction_id: ContextVar[str | None] = ContextVar(
    "requested_source_extraction_id", default=None
)
_LOGGER = logging.getLogger(__name__)


def current_extraction_id() -> str | None:
    """Return the innermost audited source-operation id for child attempt rows."""
    return _active_extraction_id.get()


@contextmanager
def source_operation_id(extraction_id: str) -> Generator[None, None, None]:
    """Bind a caller-owned id to one audited operation for idempotent retries."""
    if not extraction_id or len(extraction_id) > 128:
        raise ValueError("invalid source operation id")
    token = _requested_extraction_id.set(extraction_id)
    try:
        yield
    finally:
        _requested_extraction_id.reset(token)


def breaker_scope_id(provider: str, endpoint_profile: str = "primary", candidate: str = "primary") -> str:
    """Canonical persistent identity; child acquisition units never affect it."""
    return f"{provider}:{endpoint_profile}:{candidate}"


def set_refresh_context(run_id: str | None, task_key: str | None, cycle_id: str | None):
    return _refresh_context.set((run_id, task_key, cycle_id))


def reset_refresh_context(token: object) -> None:
    _refresh_context.reset(token)  # type: ignore[arg-type]


@contextmanager
def source_retry_scope(
    source: str, scope_id: str, profile: RetryProfile
) -> Generator[RetryBudget, None, None]:
    """Share one planned-work budget across a multi-indicator source task."""
    inherited = current_retry_budget()
    if inherited is not None:
        yield inherited
        return
    budget = RetryBudget(profile, scope_id=breaker_scope_id(source, scope_id))
    budget.request_gate = lambda: _claim_provider_probe(budget)
    token = install_retry_budget(budget)
    try:
        yield budget
    finally:
        reset_retry_budget(token)


@contextmanager
def acquisition_unit(
    unit_id: str, request_cap: int, deadline_seconds: float | None = None
) -> Generator[RetryBudget, None, None]:
    """Apply one preplanned child cap inside the source-wide root budget."""
    budget = current_retry_budget()
    if budget is None:
        raise RuntimeError("acquisition unit requires an active source budget")
    budget.set_unit_limit(unit_id, request_cap, deadline_seconds)
    token = budget.enter_unit(unit_id)
    try:
        yield budget
    finally:
        budget.leave_unit(token)


@dataclass(frozen=True, slots=True)
class FetchResult:
    value: object
    network_called: bool = True
    received_count: int | None = None
    valid_count: int | None = None
    persisted_count: int | None = None
    inserted_count: int | None = None
    updated_count: int | None = None
    reference_min: str | None = None
    reference_max: str | None = None
    source_data_date: str | None = None
    extracted_at: str | None = None
    request_count: int | None = None
    retry_count: int | None = None
    status: str | None = None
    counts_known: bool = True
    breaker_scope_id: str | None = None
    clear_primary_breaker: bool = True


class SourceOperationError(RuntimeError):
    """Provider operation failure with an explicit phase and committed-data facts."""

    def __init__(
        self,
        message: str,
        *,
        stage: str,
        received_count: int | None = None,
        valid_count: int | None = None,
        persisted_count: int | None = None,
        committed_count: int | None = None,
        source_data_date: str | None = None,
        error_category: ErrorCategory | None = None,
        retry_not_before: str | None = None,
        preserve_primary_breaker: bool = False,
    ) -> None:
        self.stage = stage
        self.received_count = received_count
        self.valid_count = valid_count
        self.persisted_count = persisted_count
        self.committed_count = committed_count
        self.source_data_date = source_data_date
        self.error_category = error_category or _category_for_stage(stage)
        self.retry_not_before = retry_not_before
        self.preserve_primary_breaker = preserve_primary_breaker
        super().__init__(message)


def as_error_payload(error: SourceOperationError) -> dict[str, object]:
    """Retain legacy indicator error shape with explicit typed outcome facts."""
    return {
        "error": getattr(error, "reason", type(error).__name__),
        "error_code": type(error).__name__,
        "stage": error.stage,
        "received_count": error.received_count,
        "valid_count": error.valid_count,
        "persisted_count": error.persisted_count,
        "committed_count": error.committed_count,
        "source_data_date": error.source_data_date,
        "error_category": error.error_category.value,
        "retry_not_before": error.retry_not_before,
    }


class SourceIngestionBase(ABC):
    source: str

    @abstractmethod
    def fetch(self, *, indicator: str, qualifiers: dict[str, object],
              operation: Callable[[], object], network_expected: bool = True) -> object:
        """Run one real provider operation through the common record boundary."""

    def _run(self, indicator: str, qualifiers: dict[str, object],
             operation: Callable[[], object], network_expected: bool) -> object:
        run_id, task_key, cycle_id = _refresh_context.get()
        started = datetime.now(timezone.utc).isoformat()
        extraction_id = _requested_extraction_id.get() or str(uuid4())
        base: dict[str, object] = {
            "extraction_id": extraction_id, "source": self.source,
            "indicator": indicator, "qualifiers_json": json.dumps(qualifiers, sort_keys=True, default=str),
            "status": "running", "network_called": int(network_expected),
            "started_at": started, "run_id": run_id, "task_key": task_key,
            "cycle_id": cycle_id,
        }
        refresh_attempt = current_refresh_attempt()
        if refresh_attempt is not None:
            base.update({
                "attempt_id": refresh_attempt.attempt_id,
                "claim_generation": refresh_attempt.generation,
                "claim_expires_at": refresh_attempt.expires_at,
            })
        extraction_started = False
        budget_token = None
        extraction_token = None
        try:
            start_source_extraction(base)
            extraction_started = True
            record_refresh_attempt_stage("extraction_started")
            inherited_budget = current_retry_budget()
            canary_candidate = qualifiers.get("candidate_id")
            if qualifiers.get("mode") == "canary" and isinstance(canary_candidate, str):
                operation_scope = breaker_scope_id(self.source, "canary", canary_candidate)
            else:
                operation_scope = breaker_scope_id(self.source)
            budget = inherited_budget or RetryBudget(
                retry_profile(self.source, qualifiers),
                scope_id=operation_scope,
            )
            budget_token = None if inherited_budget is not None else install_retry_budget(budget)
            if inherited_budget is None:
                budget.request_gate = lambda: _claim_provider_probe(budget)
            request_start = budget.requests
            retry_start = budget.retries
            extraction_token = _active_extraction_id.set(extraction_id)
        except Exception as exc:
            category = classify_exception(exc)
            try:
                if extraction_started:
                    finish_source_extraction({
                        **base,
                        "status": _error_status(exc),
                        "network_called": 0,
                        "finished_at": datetime.now(timezone.utc).isoformat(),
                        "error_code": type(exc).__name__[:80],
                        "error_detail": _safe_error_detail(exc),
                        "error_category": category.value,
                        "request_count": 0,
                        "retry_count": 0,
                    })
                record_refresh_attempt_stage("budget_setup", "error", type(exc).__name__[:80])
            except Exception:  # noqa: BLE001 - preserve the original setup failure
                _LOGGER.error("could not record source budget setup failure for %s", extraction_id)
            if extraction_token is not None:
                _active_extraction_id.reset(extraction_token)
            if budget_token is not None:
                reset_retry_budget(budget_token)
            raise
        try:
            record_refresh_attempt_stage("operation_started")
            raw = operation()
            result = raw if isinstance(raw, FetchResult) else FetchResult(raw, network_expected)
            request_count = budget.requests - request_start
            retry_count = budget.retries - retry_start
            if request_count and budget.remaining_seconds() <= 0:
                raise SourceOperationError(
                    "source acquisition deadline elapsed before outcome finalization",
                    stage="network",
                    received_count=result.received_count,
                    valid_count=result.valid_count,
                    persisted_count=result.persisted_count,
                    committed_count=result.persisted_count,
                    source_data_date=result.source_data_date,
                    error_category=ErrorCategory.DEFERRED,
                )
            value: object = result.value
            if _has_provider_error(value):
                error_value = cast(dict[str, object], value)
                category = _category_from_payload(error_value)
                status = _status_for_category(category, str(error_value.get("stage") or ""))
                _record_breaker_outcome(budget, category, error_value, request_count)
                finish_source_extraction({
                    **base, "status": status,
                    "network_called": int(network_expected),
                    "finished_at": datetime.now(timezone.utc).isoformat(),
                    "error_code": str(error_value.get("error_code") or "ProviderReportedError")[:80],
                    "error_detail": str(error_value.get("error") or status)[:120],
                    "received_count": error_value.get("received_count"),
                    "valid_count": error_value.get("valid_count"),
                    "persisted_count": error_value.get("persisted_count"),
                    "source_data_date": error_value.get("source_data_date"),
                    "error_category": category.value,
                    "retry_not_before": error_value.get("retry_not_before"),
                    "request_count": request_count,
                    "retry_count": retry_count,
                })
                return value
            values, refs = _records_and_dates(value) if result.counts_known else ([], [])
            valid_count = result.valid_count if result.valid_count is not None else (
                len(values) if result.counts_known and result.network_called else 0 if not result.network_called else None
            )
            received_count = result.received_count
            persisted_count = result.persisted_count
            if isinstance(value, dict):
                payload = cast(dict[str, object], value)
                row_count = payload.get("rows")
                if isinstance(row_count, int) and not isinstance(row_count, bool):
                    valid_count = row_count
                    received_count = row_count
                    persisted_count = row_count
            status = result.status or (
                "cache_hit" if not result.network_called else "success" if valid_count is None or valid_count else "empty"
            )
            finished = datetime.now(timezone.utc).isoformat()
            completed = {
                **base, "status": status,
                "network_called": int(result.network_called),
                "finished_at": finished,
                "extracted_at": result.extracted_at or (finished if result.network_called else None),
                "source_data_date": result.source_data_date or (max(refs) if refs else None),
                "source_reference_min": result.reference_min or (min(refs) if refs else None),
                "source_reference_max": result.reference_max or (max(refs) if refs else None),
                "received_count": received_count if received_count is not None else (
                    len(values) if result.counts_known and result.network_called else 0 if not result.network_called else None
                ),
                "valid_count": valid_count,
                "persisted_count": persisted_count,
                "inserted_count": result.inserted_count,
                "updated_count": result.updated_count,
                "request_count": request_count or result.request_count,
                "retry_count": retry_count if request_count else result.retry_count,
            }
            record_refresh_attempt_stage("operation_returned")
            if request_count and result.clear_primary_breaker and not budget.primary_failure_recorded:
                _record_breaker_success(budget, request_count)
            if request_count and result.breaker_scope_id:
                record_source_access_success(
                    result.breaker_scope_id, datetime.now(timezone.utc).isoformat()
                )
            try:
                finish_source_extraction(completed)
                record_refresh_attempt_stage("source_outcome_recorded")
            except Exception as logging_error:
                raise SourceOperationError(
                    "source extraction outcome could not be recorded",
                    stage="logging",
                    received_count=_optional_int(completed["received_count"]),
                    valid_count=_optional_int(completed["valid_count"]),
                    persisted_count=_optional_int(completed["persisted_count"]),
                    committed_count=_optional_int(completed["persisted_count"]),
                    source_data_date=_optional_string(completed["source_data_date"]),
                ) from logging_error
            return cast(object, value)
        except Exception as exc:
            category = classify_exception(exc)
            retry_not_before = getattr(exc, "retry_not_before", None) or budget.retry_not_before
            request_count = budget.requests - request_start
            retry_count = budget.retries - retry_start
            preserve_primary = budget.primary_failure_recorded and getattr(
                exc, "preserve_primary_breaker", False
            )
            if not preserve_primary or category is ErrorCategory.RATE_LIMITED:
                _record_breaker_outcome(budget, category, exc, request_count)
            failure = {
                **base,
                "status": _error_status(exc),
                "finished_at": datetime.now(timezone.utc).isoformat(),
                "error_code": str(
                    getattr(exc, "provider_error_code", type(exc).__name__)
                )[:80],
                "error_detail": _safe_error_detail(exc),
                "received_count": getattr(exc, "received_count", None),
                "valid_count": getattr(exc, "valid_count", None),
                "persisted_count": getattr(exc, "persisted_count", None),
                "source_data_date": getattr(exc, "source_data_date", None),
                "error_category": category.value,
                "retry_not_before": retry_not_before.isoformat() if isinstance(retry_not_before, datetime) else retry_not_before,
                "request_count": request_count,
                "retry_count": retry_count,
            }
            try:
                finish_source_extraction(failure)
                record_refresh_attempt_stage("source_error_recorded", "error", type(exc).__name__[:80])
            except Exception as logging_error:
                raise SourceOperationError(
                    "source extraction failure could not be recorded",
                    stage="logging",
                    received_count=_optional_int(failure["received_count"]),
                    valid_count=_optional_int(failure["valid_count"]),
                    persisted_count=_optional_int(failure["persisted_count"]),
                    committed_count=getattr(exc, "committed_count", None),
                    source_data_date=_optional_string(failure["source_data_date"]),
                ) from logging_error
            raise
        except BaseException as exc:
            try:
                record_refresh_attempt_stage("interrupted", "outcome_unknown", type(exc).__name__[:80])
            except Exception:
                _LOGGER.exception("could not record interrupted source operation %s", extraction_id)
            raise
        finally:
            _active_extraction_id.reset(extraction_token)
            if budget_token is not None:
                reset_retry_budget(budget_token)


def _category_for_stage(stage: str) -> ErrorCategory:
    return {
        "network": ErrorCategory.NETWORK_RESET,
        "validation": ErrorCategory.INVALID_SCHEMA,
        "persistence": ErrorCategory.PERSISTENCE_FAILURE,
        "freshness": ErrorCategory.FRESHNESS_FAILURE,
        "logging": ErrorCategory.LOGGING_FAILURE,
    }.get(stage, ErrorCategory.INTERNAL_ERROR)


def _optional_int(value: object) -> int | None:
    return value if isinstance(value, int) and not isinstance(value, bool) else None


def _optional_string(value: object) -> str | None:
    return value if isinstance(value, str) else None


def _category_from_payload(payload: dict[str, object]) -> ErrorCategory:
    candidate = getattr(payload, "error_category", None) or payload.get("error_category")
    if isinstance(candidate, str):
        try:
            return ErrorCategory(candidate)
        except ValueError:
            return ErrorCategory.INTERNAL_ERROR
    stage = payload.get("stage")
    return _category_for_stage(stage) if isinstance(stage, str) else ErrorCategory.INTERNAL_ERROR


def _status_for_category(category: ErrorCategory, stage: str) -> str:
    # Preserve the existing ledger status vocabulary; error_category is the
    # precise, additive classification consumed by diagnostics and policy.
    return {
        ErrorCategory.INVALID_SCHEMA: "validation_error",
        ErrorCategory.SEMANTIC_CONFLICT: "validation_error",
        ErrorCategory.INCOMPLETE_COVERAGE: "validation_error",
        ErrorCategory.PERSISTENCE_FAILURE: "persistence_error",
        ErrorCategory.FRESHNESS_FAILURE: "freshness_error",
        ErrorCategory.LOGGING_FAILURE: "logging_error",
    }.get(category, "network_error" if stage == "network" or category in {
        ErrorCategory.NETWORK_TIMEOUT, ErrorCategory.NETWORK_RESET,
        ErrorCategory.TEMPORARY_SERVER_ERROR, ErrorCategory.RATE_LIMITED,
        ErrorCategory.ENVELOPE_ERROR, ErrorCategory.DEFERRED,
        ErrorCategory.CIRCUIT_OPEN,
    } else "validation_error")


def _claim_provider_probe(budget: RetryBudget) -> None:
    now = datetime.now(timezone.utc)
    lease_until = now + timedelta(seconds=budget.profile.deadline_seconds + 15)
    admitted, retry_value, half_open = claim_source_access_probe(
        budget.scope_id, now.isoformat(), lease_until.isoformat()
    )
    if half_open:
        budget.restrict_to_probe()
    if not admitted:
        retry_at = now + timedelta(minutes=5)
        if isinstance(retry_value, str):
            try:
                parsed = datetime.fromisoformat(retry_value)
                if parsed.tzinfo is None:
                    parsed = parsed.replace(tzinfo=timezone.utc)
                retry_at = max(retry_at, parsed.astimezone(timezone.utc))
            except ValueError:
                pass
        raise CircuitOpen(retry_at)


def _record_breaker_success(budget: RetryBudget, request_count: int) -> None:
    if request_count:
        record_source_access_success(budget.scope_id, datetime.now(timezone.utc).isoformat())


def _record_breaker_outcome(
    budget: RetryBudget,
    category: ErrorCategory,
    error: BaseException | dict[str, object],
    request_count: int,
) -> None:
    if request_count == 0:
        return
    temporary_categories = {
        ErrorCategory.NETWORK_TIMEOUT, ErrorCategory.NETWORK_RESET,
        ErrorCategory.TEMPORARY_SERVER_ERROR, ErrorCategory.ENVELOPE_ERROR,
    }
    if category in temporary_categories or category is ErrorCategory.RATE_LIMITED:
        now = datetime.now(timezone.utc)
        retry_at = getattr(error, "retry_not_before", None) or budget.retry_not_before
        if not isinstance(retry_at, datetime) and isinstance(error, dict):
            raw = error.get("retry_not_before")
            if isinstance(raw, str):
                try:
                    retry_at = datetime.fromisoformat(raw)
                except ValueError:
                    retry_at = None
        if isinstance(retry_at, str):
            try:
                retry_at = datetime.fromisoformat(retry_at)
            except ValueError:
                retry_at = None
        if isinstance(retry_at, datetime):
            retry_at = retry_at.astimezone(timezone.utc)
        else:
            retry_at = None
        open_until = max(now + timedelta(minutes=5), retry_at or now)
        record_source_access_failure(
            budget.scope_id,
            now.isoformat(),
            open_until.isoformat(),
            retry_at.isoformat() if retry_at else None,
            rate_limited=category is ErrorCategory.RATE_LIMITED,
            failure_count=min(request_count, 3),
        )
    elif budget.gate_checked:
        now = datetime.now(timezone.utc)
        release_source_access_probe(
            budget.scope_id, now.isoformat(), (now + timedelta(minutes=5)).isoformat()
        )


def _error_status(error: Exception) -> str:
    stage = getattr(error, "stage", None)
    if stage == "validation":
        return "validation_error"
    if stage == "persistence":
        return "persistence_error"
    if stage == "freshness":
        return "freshness_error"
    if stage == "logging":
        return "logging_error"
    if stage == "network":
        return "network_error"
    category = classify_exception(error)
    if category in {
        ErrorCategory.NETWORK_TIMEOUT,
        ErrorCategory.NETWORK_RESET,
        ErrorCategory.TEMPORARY_SERVER_ERROR,
        ErrorCategory.RATE_LIMITED,
        ErrorCategory.ENVELOPE_ERROR,
        ErrorCategory.DEFERRED,
        ErrorCategory.CIRCUIT_OPEN,
    }:
        return "network_error"
    reason = getattr(error, "reason", None)
    if reason == "invalid_payload":
        return "validation_error"
    if reason == "provider_failure":
        return "network_error"
    name = type(error).__name__.lower()
    if "payload" in name or "validation" in name or "json" in name:
        return "validation_error"
    if "persist" in name or "database" in name or "integrity" in name:
        return "persistence_error"
    if "http" in name or "timeout" in name or "provider" in name or "url" in name:
        return "network_error"
    return "validation_error"


def _safe_error_detail(error: Exception) -> str:
    stage = getattr(error, "stage", None)
    reason = getattr(error, "reason", None)
    if isinstance(reason, str) and reason:
        return reason[:120]
    if isinstance(stage, str) and stage:
        return stage[:120]
    return type(error).__name__[:120]


def _has_provider_error(value: object) -> bool:
    return isinstance(value, dict) and bool(cast(dict[str, object], value).get("error"))


def _records_and_dates(value: object) -> tuple[list[dict[str, object]], list[str]]:
    rows: list[dict[str, object]] = []
    dates: set[str] = set()

    def visit(item: object) -> None:
        if isinstance(item, dict):
            row = cast(dict[str, object], item)
            date_value = next((row.get(key) for key in ("data", "Data", "date", "trade_date", "base_date", "DataReferencia", "reference_period") if row.get(key) is not None), None)
            if date_value is not None:
                try:
                    text = str(date_value)
                    parsed = (
                        date.fromisoformat(text[:10])
                        if "-" in text[:10]
                        else datetime.strptime(text[:10], "%d/%m/%Y").replace(
                            tzinfo=timezone.utc
                        ).date()
                    )
                    dates.add(parsed.isoformat())
                except ValueError:
                    pass
            if any(key in row for key in ("data", "Data", "date", "trade_date", "base_date", "DataReferencia", "reference_period")):
                rows.append(row)
            else:
                for child in row.values():
                    visit(child)
        elif isinstance(item, list):
            for child in cast(list[object], item):
                visit(child)

    visit(value)
    return rows, sorted(dates)


class BcbSgsIngestion(SourceIngestionBase):
    source = "bcb_sgs"

    def fetch(self, *, indicator: str, qualifiers: dict[str, object], operation: Callable[[], object], network_expected: bool = True) -> object:
        return self._run(indicator, qualifiers, operation, network_expected)


class FocusIngestion(SourceIngestionBase):
    source = "focus"

    def fetch(self, *, indicator: str, qualifiers: dict[str, object], operation: Callable[[], object], network_expected: bool = True) -> object:
        return self._run(indicator, qualifiers, operation, network_expected)


class SidraIngestion(SourceIngestionBase):
    source = "ibge_sidra"

    def fetch(self, *, indicator: str, qualifiers: dict[str, object], operation: Callable[[], object], network_expected: bool = True) -> object:
        return self._run(indicator, qualifiers, operation, network_expected)


class B3DiIngestion(SourceIngestionBase):
    source = "b3"

    def fetch(self, *, indicator: str, qualifiers: dict[str, object], operation: Callable[[], object], network_expected: bool = True) -> object:
        return self._run(indicator, qualifiers, operation, network_expected)


class AnbimaIngestion(SourceIngestionBase):
    source = "anbima"

    def fetch(self, *, indicator: str, qualifiers: dict[str, object], operation: Callable[[], object], network_expected: bool = True) -> object:
        return self._run(indicator, qualifiers, operation, network_expected)


class TreasuryIngestion(SourceIngestionBase):
    source = "tesouro"

    def fetch(self, *, indicator: str, qualifiers: dict[str, object], operation: Callable[[], object], network_expected: bool = True) -> object:
        return self._run(indicator, qualifiers, operation, network_expected)


REGISTRY: dict[str, SourceIngestionBase] = {
    adapter.source: adapter for adapter in (
        BcbSgsIngestion(), FocusIngestion(), SidraIngestion(),
        B3DiIngestion(), AnbimaIngestion(), TreasuryIngestion(),
    )
}


def audited(source: str, indicator_arg: str | int = "indicator", *,
            qualifiers: Callable[..., dict[str, object]] | None = None,
            network_expected: Callable[..., bool] | bool | None = None,
            counts_known: Callable[..., bool] | bool = True):
    """Decorate an existing source entrypoint while retaining its callable API."""
    adapter = REGISTRY[source]

    def decorate(operation: Callable[..., T]) -> Callable[..., T]:
        @wraps(operation)
        def wrapped(*args: object, **kwargs: object) -> T:
            params = cast(
                dict[str, object], dict(inspect.signature(operation).bind_partial(*args, **kwargs).arguments)
            )
            lookup_key = str(indicator_arg)
            indicator = str(params.get(lookup_key, args[0] if args and lookup_key in {"indicator", "code"} else indicator_arg))
            q = qualifiers(params) if qualifiers else {k: v for k, v in params.items() if k not in {"self"}}
            expected = network_expected(params) if callable(network_expected) else (
                network_expected if network_expected is not None else not bool(params.get("use_cache", False))
            )
            has_counts = counts_known(params) if callable(counts_known) else counts_known

            def run() -> object:
                value = operation(*args, **kwargs)
                if has_counts:
                    return value
                return FetchResult(value, network_called=expected, counts_known=False)

            return cast(T, adapter.fetch(indicator=indicator, qualifiers=q,
                                         operation=run,
                                         network_expected=expected))
        return wrapped
    return decorate
