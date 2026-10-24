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
