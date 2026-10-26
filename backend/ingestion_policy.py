"""Typed failure and bounded-retry primitives for source acquisition.

The module has no I/O at import time. Callers install a single budget around a
logical acquisition; provider HTTP helpers consume it at the actual request
boundary, including redirects.
"""

from __future__ import annotations

from collections.abc import Callable, Mapping
from contextvars import ContextVar, Token
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from email.utils import parsedate_to_datetime
from enum import StrEnum
from json import JSONDecodeError
from threading import Lock
from time import monotonic
from types import MappingProxyType
from typing import Final

import httpx

from provider_hosts import ProviderHostRejectedError


class ErrorCategory(StrEnum):
    NETWORK_TIMEOUT = "network_timeout"
    NETWORK_RESET = "network_reset"
    TEMPORARY_SERVER_ERROR = "temporary_server_error"
    RATE_LIMITED = "rate_limited"
    ENVELOPE_ERROR = "envelope_error"
    INVALID_SCHEMA = "invalid_schema"
    SEMANTIC_CONFLICT = "semantic_conflict"
    INCOMPLETE_COVERAGE = "incomplete_coverage"
    PERSISTENCE_FAILURE = "persistence_failure"
    FRESHNESS_FAILURE = "freshness_failure"
    LOGGING_FAILURE = "logging_failure"
    CONFIGURATION_ERROR = "configuration_error"
    AUTHENTICATION_ERROR = "authentication_error"
    FORBIDDEN_HOST = "forbidden_host"
    INTERNAL_ERROR = "internal_error"
    DEFERRED = "deferred"
    CIRCUIT_OPEN = "circuit_open"


class RetryPolicyError(RuntimeError):
    category: ErrorCategory = ErrorCategory.INTERNAL_ERROR


class RetryBudgetExceeded(RetryPolicyError):
    category = ErrorCategory.DEFERRED


class RetryDeferred(RetryPolicyError):
    category = ErrorCategory.DEFERRED

    def __init__(
        self,
        retry_not_before: datetime,
        reason: str = "provider retry deferred",
        *,
        cause_category: ErrorCategory = ErrorCategory.DEFERRED,
    ) -> None:
        self.retry_not_before = retry_not_before.astimezone(timezone.utc)
        self.error_category = cause_category
        super().__init__(reason)


class CircuitOpen(RetryPolicyError):
    category = ErrorCategory.CIRCUIT_OPEN

    def __init__(self, retry_not_before: datetime) -> None:
        self.retry_not_before = retry_not_before.astimezone(timezone.utc)
        super().__init__("provider circuit is open")


@dataclass(frozen=True, slots=True)
class RetryProfile:
    source: str
    max_requests: int
    deadline_seconds: float
    request_timeout_seconds: float


RETRY_PROFILES: Final[Mapping[str, RetryProfile]] = MappingProxyType({
    "bcb_sgs": RetryProfile("bcb_sgs", 3, 20.0, 20.0),
    "focus": RetryProfile("focus", 3, 45.0, 30.0),
    "ibge_sidra": RetryProfile("ibge_sidra", 3, 90.0, 30.0),
    "b3": RetryProfile("b3", 64, 240.0, 30.0),
    "anbima": RetryProfile("anbima", 3, 120.0, 120.0),
    "tesouro": RetryProfile("tesouro", 2, 90.0, 90.0),
})


@dataclass(slots=True)
class RetryBudget:
    profile: RetryProfile
    clock: Callable[[], float] = monotonic
    scope_id: str = ""
    request_gate: Callable[[], None] | None = None
    started_at: float = field(init=False)
    requests: int = 0
    retries: int = 0
    primary_failure_recorded: bool = False
    retry_not_before: datetime | None = None
    _request_limit: int = field(init=False)
    _lock: Lock = field(default_factory=Lock, init=False, repr=False)
    _gate_checked: bool = field(default=False, init=False, repr=False)
    _unit_limits: dict[str, int] = field(default_factory=dict, init=False, repr=False)
    _unit_deadlines: dict[str, float | None] = field(default_factory=dict, init=False, repr=False)
    _unit_started: dict[str, float] = field(default_factory=dict, init=False, repr=False)
    _unit_requests: dict[str, int] = field(default_factory=dict, init=False, repr=False)
    _request_group: ContextVar[str | None] = field(
        default_factory=lambda: ContextVar("source_request_group", default=None),
        init=False,
        repr=False,
    )
    _group_requests: dict[str, int] = field(default_factory=dict, init=False, repr=False)
    _unit_context: ContextVar[str | None] = field(
        default_factory=lambda: ContextVar("source_retry_unit", default=None),
        init=False,
        repr=False,
    )

    def __post_init__(self) -> None:
        self.started_at = self._now()
        self._request_limit = self.profile.max_requests

    def _now(self) -> float:
        return self.clock()

    @property
    def deadline_at(self) -> float:
        return self.started_at + self.profile.deadline_seconds

    def remaining_seconds(self) -> float:
        return max(0.0, self.deadline_at - self._now())

    @property
    def gate_checked(self) -> bool:
        return self._gate_checked

    def restrict_to_probe(self) -> None:
        self._request_limit = 1

    def reserve_request(self) -> float:
        """Count one outbound HTTP request, including each redirect hop."""
        with self._lock:
            remaining = self.remaining_seconds()
            if remaining <= 0:
                raise RetryBudgetExceeded("source acquisition deadline elapsed")
            if self.requests >= self._request_limit:
                raise RetryBudgetExceeded("source HTTP request budget exhausted")
            unit = self._unit_context.get()
            group = self._request_group.get() or unit or "root"
            if unit is not None:
                unit_policy = self._unit_limits.get(unit)
                if unit_policy is None:
                    raise RetryBudgetExceeded("unplanned source acquisition unit")
                unit_limit = unit_policy
                if self._unit_requests.get(unit, 0) >= unit_limit:
                    raise RetryBudgetExceeded("source acquisition unit request budget exhausted")
                unit_deadline = self._unit_deadlines.get(unit)
                if unit_deadline is not None:
                    remaining = min(remaining, self._unit_started[unit] + unit_deadline - self._now())
                    if remaining <= 0:
                        raise RetryBudgetExceeded("source acquisition unit deadline elapsed")
            if not self._gate_checked and self.request_gate is not None:
                self.request_gate()
                self._gate_checked = True
            self.requests += 1
            group_previous = self._group_requests.get(group, 0)
            self._group_requests[group] = group_previous + 1
            if group_previous > 0:
                self.retries += 1
            if unit is not None:
                self._unit_requests[unit] = self._unit_requests.get(unit, 0) + 1
            return remaining

    def enter_request_group(self, group: str) -> Token[str | None]:
        if not group:
            raise ValueError("request group must be non-empty")
        return self._request_group.set(group)

    def leave_request_group(self, token: Token[str | None]) -> None:
        self._request_group.reset(token)

    def set_request_limit(self, planned_requests: int) -> None:
        """Narrow the root cap to the statically derived request plan."""
        with self._lock:
            if self.requests:
                raise RuntimeError("cannot change acquisition plan after network access")
            if not 0 <= planned_requests <= self.profile.max_requests:
                raise ValueError("planned request count exceeds the approved root cap")
            self._request_limit = planned_requests

    def set_unit_limit(
        self, unit: str, planned_requests: int, deadline_seconds: float | None = None
    ) -> None:
        if not unit or planned_requests < 1:
            raise ValueError("acquisition units require an id and positive request cap")
        if deadline_seconds is not None and deadline_seconds <= 0:
            raise ValueError("acquisition unit deadline must be positive")
        with self._lock:
            if unit in self._unit_limits:
                if self._unit_limits[unit] != planned_requests or self._unit_deadlines[unit] != deadline_seconds:
                    raise ValueError("acquisition unit cannot change its planned budget")
                return
            self._unit_limits[unit] = planned_requests
            self._unit_deadlines[unit] = deadline_seconds

    def enter_unit(self, unit: str) -> Token[str | None]:
        self._unit_started.setdefault(unit, self._now())
        return self._unit_context.set(unit)

    def leave_unit(self, token: Token[str | None]) -> None:
        self._unit_context.reset(token)

    def defer(self, retry_at: datetime) -> RetryDeferred:
        retry_at = retry_at.astimezone(timezone.utc)
        if self.retry_not_before is not None:
            retry_at = max(retry_at, self.retry_not_before)
        self.retry_not_before = retry_at
        return RetryDeferred(retry_at)

    def retry_after(self, raw: str | None, *, now: datetime | None = None) -> datetime | None:
        """Parse delta-seconds or HTTP-date without shortening provider advice."""
        if not raw:
            return None
        current = now or datetime.now(timezone.utc)
        try:
            seconds = float(raw.strip())
        except (TypeError, ValueError):
            try:
                target = parsedate_to_datetime(raw)
            except (TypeError, ValueError, OverflowError):
                return None
            if target.tzinfo is None:
                target = target.replace(tzinfo=timezone.utc)
            return target.astimezone(timezone.utc) if target > current else None
        if seconds <= 0:
            return None
        try:
            return current + timedelta(seconds=seconds)
        except OverflowError:
            return datetime.max.replace(tzinfo=timezone.utc)


_active_budget: ContextVar[RetryBudget | None] = ContextVar("source_retry_budget", default=None)


def current_retry_budget() -> RetryBudget | None:
    return _active_budget.get()


def retry_profile(source: str, qualifiers: Mapping[str, object]) -> RetryProfile:
    """Return a finite policy preserving stricter provider-specific attempts."""
    profile = RETRY_PROFILES[source]
    if source == "bcb_sgs":
        annual_window = qualifiers.get("window_policy") == "calendar-year-v1"
        window_count = qualifiers.get("window_count", 1)
        count = (
            window_count
            if isinstance(window_count, int) and not isinstance(window_count, bool)
            else 1
        )
        return RetryProfile(
            source,
            count * 3,
            20.0 if annual_window and count == 1 else 220.0,
            20.0 if annual_window else 30.0,
        )
    if source == "b3":
        days = qualifiers.get("days", 30)
        bounded_days = days if isinstance(days, int) and not isinstance(days, bool) else 30
        # The collector replaces this safe bound with exactly twice its ordered,
        # deduplicated target + lookback + vintage-repair date plan before GET.
        planned_requests = max(2, (bounded_days + 2) * 2)
        return RetryProfile(source, planned_requests, 240.0, 30.0)
    return profile


def install_retry_budget(budget: RetryBudget) -> Token[RetryBudget | None]:
    return _active_budget.set(budget)


def reset_retry_budget(token: Token[RetryBudget | None]) -> None:
    _active_budget.reset(token)


def classify_exception(error: BaseException) -> ErrorCategory:
    if getattr(error, "stage", None) == "network" and error.__cause__ is not None:
        cause_category = classify_exception(error.__cause__)
        if cause_category is not ErrorCategory.INTERNAL_ERROR:
            return cause_category
    explicit = getattr(error, "error_category", None)
    if isinstance(explicit, ErrorCategory):
        return explicit
    category = getattr(error, "category", None)
    if isinstance(category, ErrorCategory):
        return category
    if isinstance(error, RetryPolicyError):
        return error.category
    if isinstance(error, ProviderHostRejectedError):
        return ErrorCategory.FORBIDDEN_HOST
    if isinstance(error, httpx.DecodingError | JSONDecodeError):
        return ErrorCategory.ENVELOPE_ERROR
    if isinstance(error, httpx.HTTPStatusError):
        status = error.response.status_code
        if status == 429:
            return ErrorCategory.RATE_LIMITED
        if status in {500, 502, 503, 504}:
            return ErrorCategory.TEMPORARY_SERVER_ERROR
        if status in {401}:
            return ErrorCategory.AUTHENTICATION_ERROR
        if status in {403}:
            return ErrorCategory.CONFIGURATION_ERROR
        return ErrorCategory.CONFIGURATION_ERROR
    if isinstance(error, httpx.TimeoutException | TimeoutError):
        return ErrorCategory.NETWORK_TIMEOUT
    if isinstance(error, httpx.NetworkError | ConnectionError | OSError):
        return ErrorCategory.NETWORK_RESET
    if getattr(error, "stage", None) == "validation":
        return ErrorCategory.INVALID_SCHEMA
    if getattr(error, "stage", None) == "persistence":
        return ErrorCategory.PERSISTENCE_FAILURE
    if getattr(error, "stage", None) == "freshness":
        return ErrorCategory.FRESHNESS_FAILURE
    if getattr(error, "stage", None) == "logging":
        return ErrorCategory.LOGGING_FAILURE
    if getattr(error, "stage", None) == "network":
        return ErrorCategory.NETWORK_RESET
    return ErrorCategory.INTERNAL_ERROR
