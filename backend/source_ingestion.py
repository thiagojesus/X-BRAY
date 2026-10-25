"""Common audited boundary for all source refresh operations.

Provider modules retain their parsers and persistence rules. Their public refresh
functions enter through a typed adapter here, so cache returns, network fetches,
validation and persistence outcomes are recorded consistently.
"""

from __future__ import annotations

import inspect
import json
from abc import ABC, abstractmethod
from collections.abc import Callable
from contextvars import ContextVar
from dataclasses import dataclass
from datetime import date, datetime, timezone
from functools import wraps
from typing import TypeVar, cast
from uuid import uuid4

from db.store import finish_source_extraction, start_source_extraction

RefreshContext = tuple[str | None, str | None, str | None]
T = TypeVar("T")
_refresh_context: ContextVar[RefreshContext] = ContextVar(
    "source_refresh_context", default=(None, None, None)
)


def set_refresh_context(run_id: str | None, task_key: str | None, cycle_id: str | None):
    return _refresh_context.set((run_id, task_key, cycle_id))


def reset_refresh_context(token: object) -> None:
    _refresh_context.reset(token)  # type: ignore[arg-type]


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
    extracted_at: str | None = None
    request_count: int | None = None
    retry_count: int | None = None
    status: str | None = None
    counts_known: bool = True


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
    ) -> None:
        self.stage = stage
        self.received_count = received_count
        self.valid_count = valid_count
        self.persisted_count = persisted_count
        self.committed_count = committed_count
        self.source_data_date = source_data_date
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
        extraction_id = str(uuid4())
        base: dict[str, object] = {
            "extraction_id": extraction_id, "source": self.source,
            "indicator": indicator, "qualifiers_json": json.dumps(qualifiers, sort_keys=True, default=str),
            "status": "running", "network_called": int(network_expected),
            "started_at": started, "run_id": run_id, "task_key": task_key,
            "cycle_id": cycle_id,
        }
        start_source_extraction(base)
        try:
            raw = operation()
            result = raw if isinstance(raw, FetchResult) else FetchResult(raw, network_expected)
            value: object = result.value
            if _has_provider_error(value):
                error_value = cast(dict[str, object], value)
                status = {
                    "network": "network_error",
                    "validation": "validation_error",
                    "persistence": "persistence_error",
                    "freshness": "freshness_error",
                    "logging": "logging_error",
                }.get(str(error_value.get("stage")), "network_error")
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
                "source_data_date": max(refs) if refs else None,
                "source_reference_min": result.reference_min or (min(refs) if refs else None),
                "source_reference_max": result.reference_max or (max(refs) if refs else None),
                "received_count": received_count if received_count is not None else (
                    len(values) if result.counts_known and result.network_called else 0 if not result.network_called else None
                ),
                "valid_count": valid_count,
                "persisted_count": persisted_count,
                "inserted_count": result.inserted_count,
                "updated_count": result.updated_count,
                "request_count": result.request_count,
                "retry_count": result.retry_count,
            }
            try:
                finish_source_extraction(completed)
            except Exception as logging_error:
                raise SourceOperationError(
                    "source extraction outcome could not be recorded",
                    stage="logging",
                    received_count=completed["received_count"],
                    valid_count=completed["valid_count"],
                    persisted_count=completed["persisted_count"],
                    committed_count=completed["persisted_count"],
                    source_data_date=completed["source_data_date"],
                ) from logging_error
            return value
        except Exception as exc:
            failure = {
                **base,
                "status": _error_status(exc),
                "finished_at": datetime.now(timezone.utc).isoformat(),
                "error_code": type(exc).__name__[:80],
                "error_detail": _safe_error_detail(exc),
                "received_count": getattr(exc, "received_count", None),
                "valid_count": getattr(exc, "valid_count", None),
                "persisted_count": getattr(exc, "persisted_count", None),
                "source_data_date": getattr(exc, "source_data_date", None),
            }
            try:
                finish_source_extraction(failure)
            except Exception as logging_error:
                raise SourceOperationError(
                    "source extraction failure could not be recorded",
                    stage="logging",
                    received_count=failure["received_count"],
                    valid_count=failure["valid_count"],
                    persisted_count=failure["persisted_count"],
                    committed_count=getattr(exc, "committed_count", None),
                    source_data_date=failure["source_data_date"],
                ) from logging_error
            raise


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
    return "network_error"


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
