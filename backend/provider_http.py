import asyncio
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from email.utils import parsedate_to_datetime
from threading import BoundedSemaphore, Lock
from time import monotonic
from typing import Final, Protocol
from urllib.parse import urljoin, urlparse

import httpx

from ingestion_policy import (
    ErrorCategory,
    RetryDeferred,
    current_retry_budget,
)
from provider_hosts import require_allowed_provider_url

JSON_MAX_BYTES: Final = 10 * 1024 * 1024
BINARY_MAX_BYTES: Final = 100 * 1024 * 1024
REDIRECT_STATUSES: Final = frozenset({301, 302, 303, 307, 308})
MAX_REDIRECTS: Final = 5
MAX_HOST_CONCURRENCY: Final = 2
_HOST_LIMITERS: Final[dict[str, BoundedSemaphore]] = {}
_HOST_LIMITERS_LOCK: Final[Lock] = Lock()


def _record_refresh_milestone(stage: str) -> None:
    from db.refresh_attempt import record_refresh_attempt_stage

    record_refresh_attempt_stage(stage)


def _host_limiter(url: str) -> BoundedSemaphore:
    hostname = urlparse(url).hostname or ""
    with _HOST_LIMITERS_LOCK:
        limiter = _HOST_LIMITERS.get(hostname)
        if limiter is None:
            limiter = BoundedSemaphore(MAX_HOST_CONCURRENCY)
            _HOST_LIMITERS[hostname] = limiter
        return limiter


class ProviderResponse(Protocol):
    @property
    def content(self) -> bytes: ...

    @property
    def url(self) -> str | httpx.URL: ...

    @property
    def status_code(self) -> int: ...

    @property
    def headers(self) -> Mapping[str, str]: ...

    def raise_for_status(self) -> object: ...


class ProviderRequest(Protocol):
    def __call__(
        self,
        url: str,
        *,
        timeout: float,
        follow_redirects: bool,
        headers: Mapping[str, str] | None,
        params: Mapping[str, str] | None,
    ) -> ProviderResponse: ...


def _trace_dispatch(
    budget: object | None,
    url: str,
    route_id: str,
    *,
    attempt_ordinal: int,
    hop_ordinal: int,
    status_code: int | None,
    started_at: str,
    bytes_received: int | None,
    dispatch_kind: str,
    retry_after: str | None = None,
    deferred_reason: str | None = None,
) -> None:
    if budget is None:
        return
    record = getattr(budget, "record_http_trace", None)
    if not callable(record):
        return
    host = urlparse(url).hostname or "unknown_host"
    record(
        route_id=route_id,
        allowed_host=host,
        attempt_ordinal=attempt_ordinal,
        hop_ordinal=hop_ordinal,
        status_code=status_code,
        dispatch_started_at=started_at,
        dispatch_finished_at=datetime.now(timezone.utc).isoformat(),
        bytes_received=bytes_received,
        dispatch_kind=dispatch_kind,
        parsed_retry_after=retry_after,
        deferred_reason=deferred_reason,
    )


def _httpx_request(
    url: str,
    *,
    timeout: float,
    follow_redirects: bool,
    headers: Mapping[str, str] | None,
    params: Mapping[str, str] | None,
) -> ProviderResponse:
    return httpx.get(
        url,
        timeout=timeout,
        follow_redirects=follow_redirects,
        headers=headers,
        params=params,
    )


@dataclass(slots=True)
class ProviderPayloadTooLargeError(ValueError):
    error_category = ErrorCategory.INVALID_SCHEMA
    limit: int
    actual: int

    def __str__(self) -> str:
        return f"provider payload exceeds {self.limit} bytes"


@dataclass(slots=True)
class ProviderInvalidJSONResponseError(ValueError):
    error_category = ErrorCategory.ENVELOPE_ERROR
    status_code: int
    content_type: str | None
    body_bytes: int

    def __str__(self) -> str:
        media_type = self.content_type or "unspecified content type"
        return (
            "provider returned an invalid JSON response "
            f"(HTTP {self.status_code}, {media_type}, {self.body_bytes} bytes)"
        )


class ProviderResponseDecodingError(httpx.DecodingError):
    """Safe response metadata for a bounded provider content-decoding failure."""

    error_category = ErrorCategory.ENVELOPE_ERROR

    def __init__(self, status_code: int, content_encoding: str | None, accept_encoding: str | None) -> None:
        self.status_code = status_code
        self.content_encoding = _safe_encoding_header(content_encoding)
        self.accept_encoding = _safe_encoding_header(accept_encoding)
        super().__init__(
            "provider response decoding failed "
            f"(HTTP {status_code}, content-encoding={self.content_encoding}, "
            f"accept-encoding={self.accept_encoding})"
        )


def _safe_encoding_header(value: str | None) -> str:
    if not value:
        return "unspecified"
    if len(value) > 64 or any(not (char.isalnum() or char in ",._- ") for char in value):
        return "unrecognized"
    return value.lower().strip() or "unspecified"


def _parse_retry_after(value: str | None) -> datetime | None:
    if not value:
        return None
    current = datetime.now(timezone.utc)
    try:
        seconds = float(value.strip())
    except (TypeError, ValueError):
        try:
            target = parsedate_to_datetime(value)
        except (TypeError, ValueError, OverflowError):
            return None
        if target.tzinfo is None:
            target = target.replace(tzinfo=timezone.utc)
        target = target.astimezone(timezone.utc)
        return target if target > current else None
    if seconds <= 0:
        return None
    try:
        return current + timedelta(seconds=seconds)
    except OverflowError:
        return datetime.max.replace(tzinfo=timezone.utc)


def get_provider_response(
    url: str,
    *,
    request: ProviderRequest = _httpx_request,
    timeout: float,
    max_bytes: int,
    follow_redirects: bool = True,
    headers: Mapping[str, str] | None = None,
    params: Mapping[str, str] | None = None,
    retry: bool = False,
    route_id: str = "provider_route",
) -> ProviderResponse:
    current_url = require_allowed_provider_url(url)
    budget = current_retry_budget()
    redirects = 0
    while True:
        request_timeout = timeout
        if budget is not None:
            request_timeout = min(timeout, budget.remaining_seconds())
        limiter = _host_limiter(current_url)
        acquired = limiter.acquire(timeout=request_timeout)
        if not acquired:
            raise TimeoutError("provider request deadline elapsed waiting for host capacity")
        try:
            if budget is not None:
                request_timeout = min(timeout, budget.reserve_request(retry=retry, redirect=redirects > 0))
            from db.refresh_attempt import mark_refresh_acquisition_started

            mark_refresh_acquisition_started()
            _record_refresh_milestone("outbound_request_started")
            dispatch_started = datetime.now(timezone.utc).isoformat()
            try:
                response = request(
                    current_url,
                    timeout=request_timeout,
                    follow_redirects=False,
                    headers=headers,
                    params=params,
                )
            except Exception:
                _trace_dispatch(
                    budget, current_url, route_id,
                    attempt_ordinal=max(1, getattr(budget, "retries", 0) + 1),
                    hop_ordinal=redirects + 1, status_code=None,
                    started_at=dispatch_started, bytes_received=None,
                    dispatch_kind="retry_initial" if retry else "failed",
                )
                raise
        finally:
            limiter.release()
        status_code = response.status_code
        location = response.headers.get("location")
        if not follow_redirects or status_code not in REDIRECT_STATUSES or not location:
            retry_after_at = _parse_retry_after(response.headers.get("retry-after"))
            body_size = len(response.content) if status_code < 300 else None
            dispatch_kind = "retry_initial" if retry else "initial"
            if status_code == 429:
                dispatch_kind = "failed"
            _trace_dispatch(
                budget, current_url, route_id,
                attempt_ordinal=max(1, getattr(budget, "retries", 0) + 1),
                hop_ordinal=redirects + 1, status_code=status_code,
                started_at=dispatch_started, bytes_received=body_size,
                dispatch_kind=dispatch_kind,
                retry_after=retry_after_at.isoformat() if retry_after_at else None,
                deferred_reason="retry_after" if status_code == 429 else None,
            )
            break
        _trace_dispatch(
            budget, current_url, route_id,
            attempt_ordinal=max(1, getattr(budget, "retries", 0) + 1),
            hop_ordinal=redirects + 1, status_code=status_code,
            started_at=dispatch_started, bytes_received=None, dispatch_kind="redirect",
        )
        redirects += 1
        if redirects > MAX_REDIRECTS:
            raise httpx.TooManyRedirects("provider exceeded redirect limit")
        current_url = require_allowed_provider_url(urljoin(current_url, str(location)))
    if budget is not None and status_code in {429, 500, 502, 503, 504}:
        retry_at = budget.retry_after(response.headers.get("retry-after"))
        if retry_at is not None:
            category = ErrorCategory.RATE_LIMITED if status_code == 429 else ErrorCategory.TEMPORARY_SERVER_ERROR
            deferred = budget.defer(retry_at)
            raise RetryDeferred(deferred.retry_not_before, cause_category=category)
    if status_code == 429:
        retry_at = _parse_retry_after(response.headers.get("retry-after")) or (
            datetime.now(timezone.utc) + timedelta(minutes=5)
        )
        if budget is not None:
            retry_at = budget.defer(retry_at).retry_not_before
        raise RetryDeferred(retry_at, cause_category=ErrorCategory.RATE_LIMITED)
    response.raise_for_status()
    require_allowed_provider_url(str(response.url))
    content = response.content
    if len(content) > max_bytes:
        raise ProviderPayloadTooLargeError(limit=max_bytes, actual=len(content))
    return response


def get_provider_response_bounded(
    url: str,
    *,
    deadline_seconds: float,
    max_bytes: int,
    params: Mapping[str, str] | None = None,
    headers: Mapping[str, str] | None = None,
    max_attempts: int = 1,
    max_retry_wait_seconds: float = 2.0,
    capture_decode_context: bool = False,
    retry: bool = False,
    route_id: str = "provider_route",
) -> httpx.Response:
    """Read a provider response under one wall-clock deadline and byte cap."""
    if deadline_seconds <= 0 or max_attempts < 1 or max_retry_wait_seconds < 0:
        raise TimeoutError("provider request deadline elapsed")

    def retry_after_seconds(response: httpx.Response) -> float | None:
        raw = response.headers.get("retry-after")
        if raw is None:
            return None
        try:
            delay = float(raw)
        except ValueError:
            try:
                retry_at = parsedate_to_datetime(raw)
            except (TypeError, ValueError, OverflowError):
                return None
            if retry_at.tzinfo is None:
                retry_at = retry_at.replace(tzinfo=timezone.utc)
            delay = (retry_at - datetime.now(timezone.utc)).total_seconds()
        return max(0.0, delay)

    async def fetch() -> httpx.Response:
        budget = current_retry_budget()
        allotted_seconds = deadline_seconds if budget is None else min(
            deadline_seconds, budget.remaining_seconds()
        )
        if allotted_seconds <= 0:
            raise TimeoutError("provider request deadline elapsed")
        deadline = monotonic() + allotted_seconds
        current_url = require_allowed_provider_url(url)
        request_headers: Mapping[str, str] = headers if headers is not None else {}
        async with asyncio.timeout(allotted_seconds):
            async with httpx.AsyncClient(
                timeout=httpx.Timeout(
                    connect=min(5.0, allotted_seconds),
                    read=min(5.0, allotted_seconds),
                    write=min(5.0, allotted_seconds),
                    pool=min(5.0, allotted_seconds),
                ),
                follow_redirects=False,
            ) as client:
                retryable_statuses = {429, 502, 503, 504}
                retryable_errors = (httpx.TimeoutException, httpx.NetworkError, httpx.DecodingError)
                attempt_limit = 1 if budget is not None and budget.half_open_probe else max_attempts
                for attempt in range(attempt_limit):
                    redirects = 0
                    retry_wait: float | None = None
                    try:
                        while True:
                            limiter = _host_limiter(current_url)
                            remaining = deadline - monotonic()
                            if remaining <= 0:
                                raise TimeoutError("provider request deadline elapsed waiting for host capacity")
                            acquired = await asyncio.to_thread(limiter.acquire, True, remaining)
                            if not acquired:
                                raise TimeoutError("provider request deadline elapsed waiting for host capacity")
                            try:
                                if budget is not None:
                                    budget.reserve_request(
                                        retry=(retry and attempt == 0 and redirects == 0)
                                        or (attempt > 0 and redirects == 0),
                                        redirect=redirects > 0,
                                    )
                                from db.refresh_attempt import (
                                    mark_refresh_acquisition_started,
                                )

                                mark_refresh_acquisition_started()
                                _record_refresh_milestone("outbound_request_started")
                                dispatch_started = datetime.now(timezone.utc).isoformat()
                                async with client.stream("GET", current_url, headers=headers, params=params) as response:
                                    _record_refresh_milestone("response_headers_received")
                                    location = response.headers.get("location")
                                    if response.status_code in REDIRECT_STATUSES and location:
                                        _trace_dispatch(
                                            budget, current_url, route_id,
                                            attempt_ordinal=(attempt + 1 if attempt else max(
                                                1, getattr(budget, "retries", 0) + 1
                                            )),
                                            hop_ordinal=redirects + 1,
                                            status_code=response.status_code,
                                            started_at=dispatch_started, bytes_received=None,
                                            dispatch_kind=(
                                                "retry_initial_redirect"
                                                if retry or attempt > 0 else "redirect"
                                            ),
                                        )
                                        redirects += 1
                                        if redirects > MAX_REDIRECTS:
                                            raise httpx.TooManyRedirects("provider exceeded redirect limit")
                                        current_url = require_allowed_provider_url(urljoin(current_url, location))
                                        continue
                                    if response.status_code == 429:
                                        retry_header = response.headers.get("retry-after")
                                        retry_at = (
                                            budget.retry_after(retry_header)
                                            if budget is not None
                                            else _parse_retry_after(retry_header)
                                        )
                                        retry_at = retry_at or (
                                            datetime.now(timezone.utc) + timedelta(minutes=5)
                                        )
                                        if budget is not None:
                                            retry_at = budget.defer(retry_at).retry_not_before
                                        _trace_dispatch(
                                            budget, current_url, route_id,
                                            attempt_ordinal=(attempt + 1 if attempt else max(
                                                1, getattr(budget, "retries", 0) + 1
                                            )),
                                            hop_ordinal=redirects + 1,
                                            status_code=429, started_at=dispatch_started,
                                            bytes_received=None, dispatch_kind="failed",
                                            retry_after=retry_at.isoformat(),
                                            deferred_reason="retry_after",
                                        )
                                        raise RetryDeferred(
                                            retry_at, cause_category=ErrorCategory.RATE_LIMITED
                                        )
                                    if response.status_code in retryable_statuses:
                                        retry_header = response.headers.get("retry-after")
                                        retry_at = (
                                            budget.retry_after(retry_header)
                                            if budget is not None
                                            else _parse_retry_after(retry_header)
                                        )
                                        if retry_at is not None:
                                            category = ErrorCategory.RATE_LIMITED if response.status_code == 429 else ErrorCategory.TEMPORARY_SERVER_ERROR
                                            retry_at = budget.defer(retry_at).retry_not_before if budget is not None else retry_at
                                            retry_wait = max(0.0, (retry_at - datetime.now(timezone.utc)).total_seconds())
                                            _trace_dispatch(
                                                budget, current_url, route_id,
                                                attempt_ordinal=(attempt + 1 if attempt else max(
                                                    1, getattr(budget, "retries", 0) + 1
                                                )),
                                                hop_ordinal=redirects + 1,
                                                status_code=response.status_code,
                                                started_at=dispatch_started, bytes_received=None,
                                                dispatch_kind="retry_initial"
                                                if attempt + 1 < attempt_limit else "failed",
                                                retry_after=retry_at.isoformat(),
                                                deferred_reason="retry_after"
                                                if attempt + 1 >= max_attempts
                                                or retry_wait > max_retry_wait_seconds
                                                or retry_wait >= deadline - monotonic()
                                                else None,
                                            )
                                            if (
                                                attempt + 1 >= max_attempts
                                                or retry_wait > max_retry_wait_seconds
                                                or retry_wait >= deadline - monotonic()
                                            ):
                                                raise RetryDeferred(retry_at, cause_category=category)
                                            break
                                    if response.status_code in retryable_statuses and attempt + 1 < attempt_limit:
                                        retry_wait = (
                                            retry_after_seconds(response) if response.status_code == 429 else 0.25
                                        )
                                        if retry_wait is not None and retry_wait <= max_retry_wait_seconds and retry_wait < deadline - monotonic():
                                            _trace_dispatch(
                                                budget, current_url, route_id,
                                                attempt_ordinal=(attempt + 1 if attempt else max(
                                                    1, getattr(budget, "retries", 0) + 1
                                                )),
                                                hop_ordinal=redirects + 1,
                                                status_code=response.status_code,
                                                started_at=dispatch_started, bytes_received=None,
                                                dispatch_kind="failed", deferred_reason=None,
                                            )
                                            break
                                    if response.status_code >= 400:
                                        _trace_dispatch(
                                            budget, current_url, route_id,
                                            attempt_ordinal=(attempt + 1 if attempt else max(
                                                1, getattr(budget, "retries", 0) + 1
                                            )),
                                            hop_ordinal=redirects + 1,
                                            status_code=response.status_code,
                                            started_at=dispatch_started, bytes_received=None,
                                            dispatch_kind="failed",
                                        )
                                    response.raise_for_status()
                                    response.raise_for_status()
                                    require_allowed_provider_url(str(response.url))
                                    body = bytearray()
                                    try:
                                        async for chunk in response.aiter_bytes(chunk_size=64 * 1024):
                                            if monotonic() >= deadline:
                                                raise TimeoutError("provider request exceeded its wall-clock deadline")
                                            if len(body) + len(chunk) > max_bytes:
                                                raise ProviderPayloadTooLargeError(max_bytes, len(body) + len(chunk))
                                            body.extend(chunk)
                                    except httpx.DecodingError as exc:
                                        if not capture_decode_context:
                                            raise
                                        raise ProviderResponseDecodingError(
                                            response.status_code,
                                            response.headers.get("content-encoding"),
                                            next((value for name, value in request_headers.items()
                                                  if name.lower() == "accept-encoding"), None),
                                        ) from exc
                                    if monotonic() >= deadline:
                                        raise TimeoutError("provider request exceeded its wall-clock deadline")
                                    _trace_dispatch(
                                        budget, current_url, route_id,
                                        attempt_ordinal=(attempt + 1 if attempt else max(
                                            1, getattr(budget, "retries", 0) + 1
                                        )),
                                        hop_ordinal=redirects + 1,
                                        status_code=response.status_code,
                                        started_at=dispatch_started, bytes_received=len(body),
                                        dispatch_kind="retry_initial"
                                        if (retry or attempt > 0) and redirects == 0
                                        else "redirect" if redirects > 0 else "initial",
                                    )
                                    return httpx.Response(response.status_code, headers=response.headers, content=bytes(body), request=response.request)
                            finally:
                                limiter.release()
                            if retry_wait is not None:
                                break
                    except retryable_errors:
                        _trace_dispatch(
                            budget, current_url, route_id,
                            attempt_ordinal=(attempt + 1 if attempt else max(
                                1, getattr(budget, "retries", 0) + 1
                            )),
                            hop_ordinal=redirects + 1, status_code=None,
                            started_at=datetime.now(timezone.utc).isoformat(),
                            bytes_received=None,
                            dispatch_kind="failed",
                        )
                        if attempt + 1 >= attempt_limit:
                            raise
                        retry_wait = min(0.25, max_retry_wait_seconds)
                    if retry_wait > max_retry_wait_seconds or retry_wait >= deadline - monotonic():
                        raise TimeoutError("provider retry deadline elapsed")
                    await asyncio.sleep(retry_wait)
                raise TimeoutError("provider request attempts exhausted")

    try:
        asyncio.get_running_loop()
    except RuntimeError:
        return asyncio.run(fetch())
    raise RuntimeError("bounded synchronous provider request cannot run inside an active event loop")
