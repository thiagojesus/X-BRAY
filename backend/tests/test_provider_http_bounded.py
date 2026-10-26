from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from threading import Thread
from time import monotonic, sleep

import pytest

import provider_http


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


def test_bounded_provider_rejects_nonpositive_deadline() -> None:
    with pytest.raises(TimeoutError, match="deadline elapsed"):
        provider_http.get_provider_response_bounded(
            "https://api.example.com/", deadline_seconds=0, max_bytes=128
        )


def test_bounded_provider_retries_retry_after_429_within_same_deadline(monkeypatch: pytest.MonkeyPatch) -> None:
    RetryHandler.attempts = 0
    server = ThreadingHTTPServer(("127.0.0.1", 0), RetryHandler)
    server.daemon_threads = True
    server.block_on_close = False
    thread = Thread(target=server.serve_forever, daemon=True)
    thread.start()
    monkeypatch.setattr(provider_http, "require_allowed_provider_url", lambda value: value)
    try:
        response = provider_http.get_provider_response_bounded(
            f"http://127.0.0.1:{server.server_port}/rate-limit",
            deadline_seconds=2, max_bytes=32, max_attempts=2,
        )
        assert response.json() == {"ok": True}
        assert RetryHandler.attempts == 2
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=1)


def test_bounded_provider_honors_treasury_30_second_retry_without_waiting_in_test(
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
        response = provider_http.get_provider_response_bounded(
            f"http://127.0.0.1:{server.server_port}/rate-limit",
            deadline_seconds=90,
            max_bytes=32,
            max_attempts=2,
            max_retry_wait_seconds=30,
        )
        assert response.json() == {"ok": True}
        assert waits == [30.0]
        assert RetryHandler.attempts == 2
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
        with pytest.raises(provider_http.httpx.HTTPStatusError):
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
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=1)


def test_provider_decode_context_sanitizes_untrusted_header_values() -> None:
    error = provider_http.ProviderResponseDecodingError(502, "<script>secret</script>", "gzip, br")
    assert error.content_encoding == "unrecognized"
    assert error.accept_encoding == "gzip, br"
    assert "secret" not in str(error)
