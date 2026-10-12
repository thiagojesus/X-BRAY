from dataclasses import dataclass, field
from concurrent.futures import ThreadPoolExecutor
from threading import Event, Lock

import pytest

from provider_hosts import ProviderHostRejectedError, require_allowed_provider_url
from provider_http import ProviderPayloadTooLargeError, get_provider_response


@dataclass
class FakeResponse:
    content: bytes
    url: str
    status_code: int = 200
    headers: dict[str, str] = field(default_factory=dict)

    def raise_for_status(self) -> None:
        return None


def test_rejects_unknown_provider_host_before_request() -> None:
    called = False

    def request(*_args, **_kwargs) -> FakeResponse:
        nonlocal called
        called = True
        return FakeResponse(b"{}", "https://attacker.test")

    with pytest.raises(ProviderHostRejectedError):
        get_provider_response(
            "https://attacker.test/data",
            request=request,
            timeout=1,
            max_bytes=10,
        )
    assert called is False


def test_rejects_uninventoried_redirect() -> None:
    def request(*_args, **_kwargs) -> FakeResponse:
        return FakeResponse(b"{}", "https://redirect.test/data")

    with pytest.raises(ProviderHostRejectedError):
        get_provider_response(
            "https://api.bcb.gov.br/data",
            request=request,
            timeout=1,
            max_bytes=10,
        )


def test_rejects_redirect_target_before_requesting_it() -> None:
    requested: list[str] = []

    def request(url: str, **_kwargs: object) -> FakeResponse:
        requested.append(url)
        return FakeResponse(
            b"",
            "https://api.bcb.gov.br/data",
            status_code=302,
            headers={"location": "http://127.0.0.1/internal"},
        )

    with pytest.raises(ProviderHostRejectedError):
        get_provider_response(
            "https://api.bcb.gov.br/data",
            request=request,
            timeout=1,
            max_bytes=10,
        )
    assert requested == ["https://api.bcb.gov.br/data"]


def test_rejects_oversized_provider_payload() -> None:
    def request(*_args, **_kwargs) -> FakeResponse:
        return FakeResponse(b"12345", "https://api.bcb.gov.br/data")

    with pytest.raises(ProviderPayloadTooLargeError):
        get_provider_response(
            "https://api.bcb.gov.br/data",
            request=request,
            timeout=1,
            max_bytes=4,
        )


def test_provider_inventory_accepts_every_current_host() -> None:
    urls = (
        "https://api.bcb.gov.br/data",
        "https://olinda.bcb.gov.br/data",
        "https://s3-data-prd-use1-precos.s3.us-east-1.amazonaws.com/data",
        "https://www.b3.com.br/data",
        "https://gamma-api.polymarket.com/data",
        "https://clob.polymarket.com/data",
        "https://www.tesourotransparente.gov.br/data",
        "https://apisidra.ibge.gov.br/data",
    )
    assert tuple(require_allowed_provider_url(url) for url in urls) == urls


def test_red_xbr_003_same_host_requests_share_two_slots() -> None:
    release = Event()
    first_two_started = Event()
    third_started = Event()
    lock = Lock()
    active = 0
    maximum = 0

    def request(url: str, **_kwargs: object) -> FakeResponse:
        nonlocal active, maximum
        with lock:
            active += 1
            maximum = max(maximum, active)
            if active >= 2:
                first_two_started.set()
            if active >= 3:
                third_started.set()
        release.wait(timeout=1)
        with lock:
            active -= 1
        return FakeResponse(b"{}", url)

    with ThreadPoolExecutor(max_workers=3) as pool:
        futures = [
            pool.submit(
                get_provider_response,
                "https://www.b3.com.br/data",
                request=request,
                timeout=1,
                max_bytes=10,
            )
            for _ in range(3)
        ]
        try:
            assert first_two_started.wait(timeout=1)
            assert not third_started.wait(timeout=0.1)
        finally:
            release.set()
        for future in futures:
            future.result(timeout=1)

    assert maximum == 2
