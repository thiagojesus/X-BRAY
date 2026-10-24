import asyncio
from collections.abc import Mapping
from dataclasses import dataclass
from threading import BoundedSemaphore, Lock
from time import monotonic
from typing import Final, Protocol
from urllib.parse import urljoin, urlparse

import httpx

from provider_hosts import require_allowed_provider_url

JSON_MAX_BYTES: Final = 10 * 1024 * 1024
BINARY_MAX_BYTES: Final = 100 * 1024 * 1024
REDIRECT_STATUSES: Final = frozenset({301, 302, 303, 307, 308})
MAX_REDIRECTS: Final = 5
MAX_HOST_CONCURRENCY: Final = 2
_HOST_LIMITERS: Final[dict[str, BoundedSemaphore]] = {}
_HOST_LIMITERS_LOCK: Final[Lock] = Lock()


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
    limit: int
    actual: int

    def __str__(self) -> str:
        return f"provider payload exceeds {self.limit} bytes"


@dataclass(slots=True)
class ProviderInvalidJSONResponseError(ValueError):
    status_code: int
    content_type: str | None
    body_bytes: int

    def __str__(self) -> str:
        media_type = self.content_type or "unspecified content type"
        return (
            "provider returned an invalid JSON response "
            f"(HTTP {self.status_code}, {media_type}, {self.body_bytes} bytes)"
        )


def get_provider_response(
    url: str,
    *,
    request: ProviderRequest = _httpx_request,
    timeout: float,
    max_bytes: int,
    follow_redirects: bool = True,
    headers: Mapping[str, str] | None = None,
    params: Mapping[str, str] | None = None,
) -> ProviderResponse:
    current_url = require_allowed_provider_url(url)
    redirects = 0
    while True:
        with _host_limiter(current_url):
            response = request(
                current_url,
                timeout=timeout,
                follow_redirects=False,
                headers=headers,
                params=params,
            )
        status_code = response.status_code
        location = response.headers.get("location")
        if not follow_redirects or status_code not in REDIRECT_STATUSES or not location:
            break
        redirects += 1
        if redirects > MAX_REDIRECTS:
            raise httpx.TooManyRedirects("provider exceeded redirect limit")
        current_url = require_allowed_provider_url(urljoin(current_url, str(location)))
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
) -> httpx.Response:
    """Read a provider response under one wall-clock deadline and byte cap."""
    if deadline_seconds <= 0:
        raise TimeoutError("provider request deadline elapsed")

    async def fetch() -> httpx.Response:
        deadline = monotonic() + deadline_seconds
        current_url = require_allowed_provider_url(url)
        async with asyncio.timeout(deadline_seconds):
            async with httpx.AsyncClient(
                timeout=httpx.Timeout(
                    connect=min(5.0, deadline_seconds),
                    read=min(5.0, deadline_seconds),
                    write=min(5.0, deadline_seconds),
                    pool=min(5.0, deadline_seconds),
                ),
                follow_redirects=False,
            ) as client:
                redirects = 0
                while True:
                    limiter = _host_limiter(current_url)
                    remaining = deadline - monotonic()
                    if remaining <= 0:
                        raise TimeoutError("provider request deadline elapsed waiting for host capacity")
                    acquired = await asyncio.to_thread(limiter.acquire, True, remaining)
                    if not acquired:
                        raise TimeoutError("provider request deadline elapsed waiting for host capacity")
                    try:
                        async with client.stream(
                            "GET", current_url, headers=headers, params=params
                        ) as response:
                            location = response.headers.get("location")
                            if response.status_code in REDIRECT_STATUSES and location:
                                redirects += 1
                                if redirects > MAX_REDIRECTS:
                                    raise httpx.TooManyRedirects("provider exceeded redirect limit")
                                current_url = require_allowed_provider_url(urljoin(current_url, location))
                                continue
                            response.raise_for_status()
                            require_allowed_provider_url(str(response.url))
                            body = bytearray()
                            async for chunk in response.aiter_bytes(chunk_size=64 * 1024):
                                if monotonic() >= deadline:
                                    raise TimeoutError("provider request exceeded its wall-clock deadline")
                                if len(body) + len(chunk) > max_bytes:
                                    raise ProviderPayloadTooLargeError(max_bytes, len(body) + len(chunk))
                                body.extend(chunk)
                            if monotonic() >= deadline:
                                raise TimeoutError("provider request exceeded its wall-clock deadline")
                            return httpx.Response(
                                response.status_code,
                                headers=response.headers,
                                content=bytes(body),
                                request=response.request,
                            )
                    finally:
                        limiter.release()

    try:
        asyncio.get_running_loop()
    except RuntimeError:
        return asyncio.run(fetch())
    raise RuntimeError("bounded synchronous provider request cannot run inside an active event loop")
