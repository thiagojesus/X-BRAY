from collections.abc import Mapping
from dataclasses import dataclass
from typing import Final, Protocol
from urllib.parse import urljoin

import httpx

from provider_hosts import require_allowed_provider_url

JSON_MAX_BYTES: Final = 10 * 1024 * 1024
BINARY_MAX_BYTES: Final = 100 * 1024 * 1024
REDIRECT_STATUSES: Final = frozenset({301, 302, 303, 307, 308})
MAX_REDIRECTS: Final = 5


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
