from datetime import datetime, timedelta, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from threading import Thread
from time import monotonic, sleep

import httpx
import pytest

import provider_http
from ingestion_policy import (
    RetryBudget,
    RetryDeferred,
    RetryProfile,
    install_retry_budget,
    reset_retry_budget,
)


class QuickHandler(BaseHTTPRequestHandler):
    def do_GET(self) -> None:
        if self.path == "/redirect":
            self.send_response(302)
            self.send_header("Location", "/fast")
            self.end_headers()
            return
        body = b'{"ok":true}' if self.path == "/fast" else b"xx"
        self.send_response(200)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, _format: str, *_args: object) -> None:
        return


class RetryHandler(BaseHTTPRequestHandler):
    attempts = 0
    retry_after = "0"

    def do_GET(self) -> None:
        type(self).attempts += 1
        if type(self).attempts == 1:
            self.send_response(429)
            self.send_header("Retry-After", type(self).retry_after)
            self.send_header("Content-Length", "0")
            self.end_headers()
            return
        body = b'{"ok":true}'
        self.send_response(200)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, _format: str, *_args: object) -> None:
        return


class InvalidCompressionHandler(BaseHTTPRequestHandler):
    attempts = 0

    def do_GET(self) -> None:
        type(self).attempts += 1
        body = b'{"not":"gzip"}'
        self.send_response(200)
        self.send_header("Content-Encoding", "gzip")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, _format: str, *_args: object) -> None:
        return


def test_bounded_provider_cancels_a_response_that_drips_before_idle_timeout(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class SlowHandler(BaseHTTPRequestHandler):
        def do_GET(self) -> None:
            body = b"x" * 100
            self.send_response(200)
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            try:
                for byte in body:
                    self.wfile.write(bytes([byte]))
                    self.wfile.flush()
                    sleep(0.04)
            except (BrokenPipeError, ConnectionResetError):
                pass

        def log_message(self, _format: str, *_args: object) -> None:
            return

    server = ThreadingHTTPServer(("127.0.0.1", 0), SlowHandler)
    server.daemon_threads = True
    server.block_on_close = False
    server_thread = Thread(target=server.serve_forever, daemon=True)
    server_thread.start()
    monkeypatch.setattr(provider_http, "require_allowed_provider_url", lambda value: value)
    started = monotonic()

    try:
        with pytest.raises(TimeoutError):
            provider_http.get_provider_response_bounded(
                f"http://127.0.0.1:{server.server_port}/slow",
                deadline_seconds=0.2,
                max_bytes=1024,
            )
    finally:
        server.shutdown()
        server.server_close()
        server_thread.join(timeout=1)

    assert monotonic() - started < 1.5


def test_bounded_provider_follows_validated_redirect_and_caps_payload(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    server = ThreadingHTTPServer(("127.0.0.1", 0), QuickHandler)
    server.daemon_threads = True
    server.block_on_close = False
    server_thread = Thread(target=server.serve_forever, daemon=True)
    server_thread.start()
    monkeypatch.setattr(provider_http, "require_allowed_provider_url", lambda value: value)
    base_url = f"http://127.0.0.1:{server.server_port}"

    try:
        result = provider_http.get_provider_response_bounded(
            f"{base_url}/redirect", deadline_seconds=1, max_bytes=32
        )
        assert result.json() == {"ok": True}
        with pytest.raises(provider_http.ProviderPayloadTooLargeError):
            provider_http.get_provider_response_bounded(
                f"{base_url}/large", deadline_seconds=1, max_bytes=1
            )
    finally:
        server.shutdown()
        server.server_close()
        server_thread.join(timeout=1)


def test_half_open_treasury_redirect_is_one_probe_and_not_counted_as_retry(monkeypatch) -> None:
    server = ThreadingHTTPServer(("127.0.0.1", 0), QuickHandler)
    server.daemon_threads = True
    server.block_on_close = False
    thread = Thread(target=server.serve_forever, daemon=True)
    thread.start()
    monkeypatch.setattr(provider_http, "require_allowed_provider_url", lambda value: value)
    budget = RetryBudget(RetryProfile("tesouro", 2, 90, 60))
    budget.restrict_to_probe()
    token = install_retry_budget(budget)
    try:
        response = provider_http.get_provider_response_bounded(
            f"http://127.0.0.1:{server.server_port}/redirect",
            deadline_seconds=2, max_bytes=32, max_attempts=2,
        )
        assert response.json() == {"ok": True}
        assert (budget.requests, budget.retries) == (2, 0)
        assert [row["dispatch_kind"] for row in budget.http_trace] == ["redirect", "redirect"]
        assert [row["bytes_received"] for row in budget.http_trace] == [None, len(b'{"ok":true}')]
        serialized = str(budget.http_trace)
        assert "authorization" not in serialized.lower()
        assert "http://" not in serialized
    finally:
        reset_retry_budget(token)
        server.shutdown()
        server.server_close()
        thread.join(timeout=1)


def test_retry_timeout_then_redirect_uses_three_dispatches_one_retry_and_ordered_trace(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class Handler(BaseHTTPRequestHandler):
        requests = 0

        def do_GET(self) -> None:
            type(self).requests += 1
            if self.path == "/retry":
                self.send_response(302)
                self.send_header("Location", "/final")
                self.end_headers()
                return
            body = b'{"ok":true}'
            self.send_response(200)
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, _format: str, *_args: object) -> None:
            return

    class TimeoutThenLocalTransport(httpx.AsyncBaseTransport):
        def __init__(self) -> None:
            self.calls = 0
            self.local = httpx.AsyncHTTPTransport()

        async def handle_async_request(self, request: httpx.Request) -> httpx.Response:
            self.calls += 1
            if self.calls == 1:
                raise httpx.ReadTimeout("simulated first dispatch timeout", request=request)
            return await self.local.handle_async_request(request)

        async def aclose(self) -> None:
            await self.local.aclose()

    Handler.requests = 0
    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    server.daemon_threads = True
    server.block_on_close = False
    thread = Thread(target=server.serve_forever, daemon=True)
    thread.start()
    local_url = f"http://127.0.0.1:{server.server_port}/retry"
    monkeypatch.setattr(
        provider_http, "require_allowed_provider_url",
        lambda value: local_url if value.startswith("https://") else value,
    )
    transport = TimeoutThenLocalTransport()
    original_client = httpx.AsyncClient
    monkeypatch.setattr(httpx, "AsyncClient", lambda **kwargs: original_client(transport=transport, **kwargs))
    budget = RetryBudget(RetryProfile("bcb_sgs", 3, 20, 2))
    token = install_retry_budget(budget)
    try:
        response = provider_http.get_provider_response_bounded(
            "https://api.bcb.gov.br/fake", deadline_seconds=3,
            max_bytes=32, max_attempts=2, route_id="bcb_sgs_window",
        )
        assert response.json() == {"ok": True}
    finally:
        reset_retry_budget(token)
        server.shutdown()
        server.server_close()
        thread.join(timeout=1)

    assert (transport.calls, Handler.requests) == (3, 2)
    assert (budget.requests, budget.retries) == (3, 1)
    assert [(entry["attempt_ordinal"], entry["hop_ordinal"], entry["dispatch_kind"])
            for entry in budget.http_trace] == [
        (1, 1, "failed"),
        (2, 1, "retry_initial_redirect"),
        (2, 2, "redirect"),
    ]


def test_half_open_timeout_preserves_read_timeout_and_never_retries(monkeypatch) -> None:
    class SlowHandler(BaseHTTPRequestHandler):
        def do_GET(self) -> None:
            sleep(0.2)
            self.send_response(200)
            self.end_headers()
            self.wfile.write(b"{}")

        def log_message(self, _format: str, *_args: object) -> None:
            return

    server = ThreadingHTTPServer(("127.0.0.1", 0), SlowHandler)
    server.daemon_threads = True
    server.block_on_close = False
    thread = Thread(target=server.serve_forever, daemon=True)
    thread.start()
    monkeypatch.setattr(provider_http, "require_allowed_provider_url", lambda value: value)
    budget = RetryBudget(RetryProfile("bcb_sgs", 3, 20, 0.05))
    budget.restrict_to_probe()
    token = install_retry_budget(budget)
    try:
        with pytest.raises(Exception) as raised:
            provider_http.get_provider_response_bounded(
                f"http://127.0.0.1:{server.server_port}/slow",
                deadline_seconds=0.05, max_bytes=32, max_attempts=3,
            )
        assert type(raised.value).__name__ in {"ReadTimeout", "TimeoutError"}
        assert budget.requests == 1
        assert budget.retries == 0
    finally:
        reset_retry_budget(token)
        server.shutdown()
        server.server_close()
        thread.join(timeout=1)


def test_half_open_redirect_then_429_preserves_quota_without_third_request(monkeypatch) -> None:
    class Redirect429Handler(BaseHTTPRequestHandler):
        requests = 0

        def do_GET(self) -> None:
            type(self).requests += 1
            if self.path == "/redirect":
                self.send_response(302)
                self.send_header("Location", "/quota")
                self.end_headers()
                return
            self.send_response(429)
            self.send_header("Retry-After", "3600")
            self.send_header("Content-Length", "0")
            self.end_headers()

        def log_message(self, _format: str, *_args: object) -> None:
            return

    Redirect429Handler.requests = 0
    server = ThreadingHTTPServer(("127.0.0.1", 0), Redirect429Handler)
    server.daemon_threads = True
    server.block_on_close = False
    thread = Thread(target=server.serve_forever, daemon=True)
    thread.start()
    monkeypatch.setattr(provider_http, "require_allowed_provider_url", lambda value: value)
    budget = RetryBudget(RetryProfile("tesouro", 2, 90, 60))
    budget.restrict_to_probe()
    token = install_retry_budget(budget)
    try:
        with pytest.raises(RetryDeferred):
            provider_http.get_provider_response_bounded(
                f"http://127.0.0.1:{server.server_port}/redirect",
                deadline_seconds=90, max_bytes=32, max_attempts=2,
                max_retry_wait_seconds=60,
            )
        assert Redirect429Handler.requests == budget.requests == 2
        assert budget.retries == 0
        assert budget.retry_not_before is not None
    finally:
        reset_retry_budget(token)
        server.shutdown()
        server.server_close()
        thread.join(timeout=1)


def test_bounded_provider_rejects_nonpositive_deadline() -> None:
    with pytest.raises(TimeoutError, match="deadline elapsed"):
        provider_http.get_provider_response_bounded(
            "https://api.example.com/", deadline_seconds=0, max_bytes=128
        )


def test_bounded_provider_defers_retry_after_429_without_second_dispatch(monkeypatch: pytest.MonkeyPatch) -> None:
    RetryHandler.attempts = 0
    server = ThreadingHTTPServer(("127.0.0.1", 0), RetryHandler)
    server.daemon_threads = True
    server.block_on_close = False
    thread = Thread(target=server.serve_forever, daemon=True)
    thread.start()
    monkeypatch.setattr(provider_http, "require_allowed_provider_url", lambda value: value)
    try:
        with pytest.raises(RetryDeferred) as raised:
            provider_http.get_provider_response_bounded(
                f"http://127.0.0.1:{server.server_port}/rate-limit",
                deadline_seconds=2, max_bytes=32, max_attempts=2,
            )
        assert raised.value.error_category.value == "rate_limited"
        assert raised.value.retry_not_before > datetime.now(timezone.utc)
        assert RetryHandler.attempts == 1
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=1)


def test_bounded_provider_defers_treasury_30_second_advice_without_sleep_or_retry(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    RetryHandler.attempts = 0
    RetryHandler.retry_after = "30"
    waits: list[float] = []

    async def record_wait(seconds: float) -> None:
        waits.append(seconds)

    server = ThreadingHTTPServer(("127.0.0.1", 0), RetryHandler)
    server.daemon_threads = True
    server.block_on_close = False
    thread = Thread(target=server.serve_forever, daemon=True)
    thread.start()
    monkeypatch.setattr(provider_http, "require_allowed_provider_url", lambda value: value)
    monkeypatch.setattr(provider_http.asyncio, "sleep", record_wait)
    try:
        with pytest.raises(RetryDeferred) as raised:
            provider_http.get_provider_response_bounded(
                f"http://127.0.0.1:{server.server_port}/rate-limit",
                deadline_seconds=90,
                max_bytes=32,
                max_attempts=2,
                max_retry_wait_seconds=30,
            )
        assert raised.value.retry_not_before >= datetime.now(timezone.utc) + timedelta(seconds=29)
        assert waits == []
        assert RetryHandler.attempts == 1
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=1)
        RetryHandler.retry_after = "0"


def test_bounded_provider_does_not_retry_when_retry_after_exceeds_budget(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    RetryHandler.attempts = 0
    RetryHandler.retry_after = "30"
    waits: list[float] = []

    async def record_wait(seconds: float) -> None:
        waits.append(seconds)

    server = ThreadingHTTPServer(("127.0.0.1", 0), RetryHandler)
    server.daemon_threads = True
    server.block_on_close = False
    thread = Thread(target=server.serve_forever, daemon=True)
    thread.start()
    monkeypatch.setattr(provider_http, "require_allowed_provider_url", lambda value: value)
    monkeypatch.setattr(provider_http.asyncio, "sleep", record_wait)
    try:
        with pytest.raises(RetryDeferred):
            provider_http.get_provider_response_bounded(
                f"http://127.0.0.1:{server.server_port}/rate-limit",
                deadline_seconds=2,
                max_bytes=32,
                max_attempts=2,
                max_retry_wait_seconds=30,
            )
        assert waits == []
        assert RetryHandler.attempts == 1
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=1)
        RetryHandler.retry_after = "0"


def test_bounded_provider_persists_final_attempt_retry_after(monkeypatch: pytest.MonkeyPatch) -> None:
    RetryHandler.attempts = 0
    RetryHandler.retry_after = "3600"
    server = ThreadingHTTPServer(("127.0.0.1", 0), RetryHandler)
    server.daemon_threads = True
    server.block_on_close = False
    thread = Thread(target=server.serve_forever, daemon=True)
    thread.start()
    monkeypatch.setattr(provider_http, "require_allowed_provider_url", lambda value: value)
    budget = RetryBudget(RetryProfile("tesouro", 2, 90, 90))
    token = install_retry_budget(budget)
    try:
        with pytest.raises(RetryDeferred) as raised:
            provider_http.get_provider_response_bounded(
                f"http://127.0.0.1:{server.server_port}/rate-limit",
                deadline_seconds=90, max_bytes=32, max_attempts=2,
            )
        assert raised.value.retry_not_before == budget.retry_not_before
        assert budget.retry_not_before is not None
        assert budget.retry_not_before >= datetime.now(timezone.utc) + timedelta(minutes=59)
        assert RetryHandler.attempts == 1
    finally:
        reset_retry_budget(token)
        server.shutdown()
        server.server_close()
        thread.join(timeout=1)
        RetryHandler.retry_after = "0"


def test_bounded_provider_retries_invalid_compression_then_preserves_decoding_error(monkeypatch: pytest.MonkeyPatch) -> None:
    InvalidCompressionHandler.attempts = 0
    server = ThreadingHTTPServer(("127.0.0.1", 0), InvalidCompressionHandler)
    server.daemon_threads = True
    server.block_on_close = False
    thread = Thread(target=server.serve_forever, daemon=True)
    thread.start()
    monkeypatch.setattr(provider_http, "require_allowed_provider_url", lambda value: value)
    try:
        with pytest.raises(provider_http.httpx.DecodingError):
            provider_http.get_provider_response_bounded(
                f"http://127.0.0.1:{server.server_port}/invalid-gzip",
                deadline_seconds=2, max_bytes=32, max_attempts=2,
            )
        assert InvalidCompressionHandler.attempts == 2
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=1)


def test_bounded_provider_can_capture_sanitized_decode_context(monkeypatch: pytest.MonkeyPatch) -> None:
    InvalidCompressionHandler.attempts = 0
    server = ThreadingHTTPServer(("127.0.0.1", 0), InvalidCompressionHandler)
    server.daemon_threads = True
    server.block_on_close = False
    thread = Thread(target=server.serve_forever, daemon=True)
    thread.start()
    monkeypatch.setattr(provider_http, "require_allowed_provider_url", lambda value: value)
    budget = RetryBudget(RetryProfile("bcb_sgs", 3, 20, 2))
    token = install_retry_budget(budget)
    try:
        with pytest.raises(provider_http.ProviderResponseDecodingError) as raised:
            provider_http.get_provider_response_bounded(
                f"http://127.0.0.1:{server.server_port}/invalid-gzip",
                deadline_seconds=2, max_bytes=32, max_attempts=2,
                headers={"Accept-Encoding": "identity"}, capture_decode_context=True,
            )
        assert raised.value.status_code == 200
        assert raised.value.content_encoding == "gzip"
        assert raised.value.accept_encoding == "identity"
        assert "invalid-gzip" not in str(raised.value)
        assert InvalidCompressionHandler.attempts == 2
        assert len(budget.http_trace) == 2
        assert all(entry["status_code"] == 200 for entry in budget.http_trace)
        assert all(entry["diagnostics"]["duration_ms"] >= 0 for entry in budget.http_trace)
    finally:
        reset_retry_budget(token)
        server.shutdown()
        server.server_close()
        thread.join(timeout=1)


def test_provider_decode_context_sanitizes_untrusted_header_values() -> None:
    error = provider_http.ProviderResponseDecodingError(502, "<script>secret</script>", "gzip, br")
    assert error.content_encoding == "unrecognized"
    assert error.accept_encoding == "gzip, br"
    assert "secret" not in str(error)


def test_diagnostic_formatter_failure_preserves_primary_request_exception(monkeypatch) -> None:
    monkeypatch.setattr(provider_http, "require_allowed_provider_url", lambda value: value)
    monkeypatch.setattr(provider_http, "build_diagnostic", lambda **_kwargs: (_ for _ in ()).throw(RuntimeError("formatter")))
    primary = httpx.ReadTimeout("primary")

    def request(_url: str, **_kwargs):
        raise primary

    budget = RetryBudget(RetryProfile("bcb_sgs", 3, 20, 2))
    token = install_retry_budget(budget)
    try:
        with pytest.raises(httpx.ReadTimeout) as raised:
            provider_http.get_provider_response(
                "https://fixture.example.invalid/data", request=request,
                timeout=1, max_bytes=10,
            )
        assert raised.value is primary
    finally:
        reset_retry_budget(token)


def test_diagnostic_formatter_failure_preserves_success_response(monkeypatch) -> None:
    monkeypatch.setattr(provider_http, "require_allowed_provider_url", lambda value: value)
    monkeypatch.setattr(provider_http, "build_diagnostic", lambda **_kwargs: (_ for _ in ()).throw(RuntimeError("formatter")))
    response = httpx.Response(200, content=b"payload", request=httpx.Request("GET", "https://fixture.example.invalid/data"))

    def request(_url: str, **_kwargs):
        return response

    budget = RetryBudget(RetryProfile("bcb_sgs", 3, 20, 2))
    token = install_retry_budget(budget)
    try:
        actual = provider_http.get_provider_response(
            "https://fixture.example.invalid/data", request=request, timeout=1, max_bytes=10,
        )
        assert actual is response
        assert actual.content == b"payload"
        assert budget.http_trace[0]["diagnostics"]["failure_code"] == "diagnostics_unavailable"
    finally:
        reset_retry_budget(token)


def test_sync_trace_duration_uses_monotonic_elapsed_not_wall_clock(monkeypatch) -> None:
    monkeypatch.setattr(provider_http, "require_allowed_provider_url", lambda value: value)
    ticks = iter((10.0, 10.375))
    monkeypatch.setattr(provider_http, "monotonic", lambda: next(ticks))
    budget = RetryBudget(RetryProfile("bcb_sgs", 3, 20, 2))
    token = install_retry_budget(budget)

    def request(url: str, **_kwargs):
        return httpx.Response(200, content=b"ok", request=httpx.Request("GET", url))

    try:
        response = provider_http.get_provider_response(
            "https://fixture.example.invalid/data", request=request, timeout=2, max_bytes=10,
        )
        assert response.content == b"ok"
        assert budget.http_trace[0]["diagnostics"]["duration_ms"] == 375.0
    finally:
        reset_retry_budget(token)


def test_async_trace_duration_uses_monotonic_when_wall_clock_moves_back(monkeypatch) -> None:
    class BackwardsClock:
        values = iter((datetime(2026, 10, 29, tzinfo=timezone.utc), datetime(2026, 10, 28, tzinfo=timezone.utc)))

        @classmethod
        def now(cls, _timezone):
            return next(cls.values)

    class Clock:
        value = 0.0

        @classmethod
        def tick(cls):
            cls.value += 0.1
            return cls.value

    async def respond(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, content=b"ok", request=request)

    original_client = httpx.AsyncClient
    monkeypatch.setattr(provider_http, "datetime", BackwardsClock)
    monkeypatch.setattr(provider_http, "monotonic", Clock.tick)
    monkeypatch.setattr(httpx, "AsyncClient", lambda **kwargs: original_client(transport=httpx.MockTransport(respond), **kwargs))
    monkeypatch.setattr(provider_http, "require_allowed_provider_url", lambda value: value)
    budget = RetryBudget(RetryProfile("bcb_sgs", 3, 20, 2))
    token = install_retry_budget(budget)
    try:
        response = provider_http.get_provider_response_bounded(
            "https://fixture.example.invalid/data", deadline_seconds=3, max_bytes=10,
        )
        assert response.content == b"ok"
        duration = budget.http_trace[0]["diagnostics"]["duration_ms"]
        assert isinstance(duration, float)
        assert duration == 300.0
        assert budget.http_trace[0]["dispatch_finished_at"] < budget.http_trace[0]["dispatch_started_at"]
    finally:
        reset_retry_budget(token)
