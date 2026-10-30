"""Safe, bounded diagnostic metadata for provider acquisition failures.

This module deliberately never serializes exception messages or response bodies.
"""

from __future__ import annotations

import hashlib
import json
import math
import socket
import ssl
from contextlib import contextmanager
from collections.abc import Generator, Mapping
from contextvars import ContextVar, Token
from dataclasses import dataclass
from datetime import date
from typing import Final, cast

import httpx

DIAGNOSTICS_VERSION: Final = "source-request-diagnostics-v1"
MAX_CAUSES: Final = 4
MAX_BODY_BYTES: Final = 10 * 1024 * 1024
_SAFE_TYPES: Final = {
    "ConnectTimeout": "connect_timeout",
    "ReadTimeout": "read_timeout",
    "WriteTimeout": "write_timeout",
    "PoolTimeout": "pool_timeout",
    "ConnectError": "connect_error",
    "ReadError": "read_error",
    "WriteError": "write_error",
    "RemoteProtocolError": "protocol_error",
    "DecodingError": "content_decoding_error",
    "HTTPStatusError": "http_status_error",
    "JSONDecodeError": "invalid_json",
    "InvalidSGSPayloadError": "invalid_json_shape",
    "RuntimeError": "unknown_runtime_error",
    "TimeoutError": "timeout",
    "ConnectionResetError": "connection_reset",
    "gaierror": "dns_resolution_error",
    "SSLError": "tls_error",
}
_PHASES: Final = frozenset({"request", "response_read", "decode", "host_capacity_wait"})
_MIME_TYPES: Final = frozenset({
    "application/json", "application/problem+json", "application/octet-stream",
    "text/plain", "text/html", "application/xml", "text/xml",
})
_ENVELOPE_KEYS: Final = frozenset({"error", "erro", "message", "mensagem", "code", "codigo", "status", "detail", "data"})


@dataclass(frozen=True, slots=True)
class SourceRequestContext:
    provider: str
    series_code: int | None = None
    requested_start: str | None = None
    requested_end: str | None = None
    trade_date: str | None = None
    file_name: str | None = None
    local_attempt: int = 1
    expected_kind: str | None = None


_request_context: ContextVar[SourceRequestContext | None] = ContextVar("source_request_diagnostic_context", default=None)


def _valid_iso_date(value: str | None) -> bool:
    if value is None:
        return False
    try:
        return date.fromisoformat(value).isoformat() == value
    except ValueError:
        return False


@contextmanager
def source_request_context(
    *, provider: str, series_code: int | None = None,
    requested_start: str | None = None, requested_end: str | None = None,
    trade_date: str | None = None, file_name: str | None = None,
    local_attempt: int = 1,
) -> Generator[SourceRequestContext | None, None, None]:
    """Set a validated, immutable per-dispatch identity and restore nesting."""
    context: SourceRequestContext | None = None
    if type(local_attempt) is int and 1 <= local_attempt <= 16:
        if provider == "bcb_sgs" and type(series_code) is int and series_code > 0 and _valid_iso_date(requested_start) and _valid_iso_date(requested_end):
            start = date.fromisoformat(requested_start or "")
            end = date.fromisoformat(requested_end or "")
            if start <= end:
                context = SourceRequestContext(provider, series_code, requested_start, requested_end, local_attempt=local_attempt, expected_kind="list")
        elif provider == "b3" and _valid_iso_date(trade_date) and isinstance(file_name, str):
            day = date.fromisoformat(trade_date or "")
            expected = f"SPRD{day:%y%m%d}.zip"
            if file_name == expected:
                context = SourceRequestContext(provider, trade_date=trade_date, file_name=expected, local_attempt=local_attempt)
    current: Token[SourceRequestContext | None] = _request_context.set(context)
    try:
        yield context
    finally:
        _request_context.reset(current)


def current_request_context() -> SourceRequestContext | None:
    return _request_context.get()


def serialize_request_context(context: SourceRequestContext | None) -> dict[str, object] | None:
    if context is None:
        return None
    result: dict[str, object] = {"provider": context.provider, "local_attempt": context.local_attempt}
    if context.provider == "bcb_sgs":
        result.update({"series_code": context.series_code, "requested_start": context.requested_start, "requested_end": context.requested_end, "expected_kind": "list"})
    elif context.provider == "b3":
        result.update({"trade_date": context.trade_date, "file_name": context.file_name})
    return result


def _sanitize_request_context(value: object) -> dict[str, object] | None:
    if not isinstance(value, Mapping):
        return None
    data = cast(Mapping[str, object], value)
    provider = data.get("provider")
    attempt = data.get("local_attempt")
    if type(attempt) is not int or not 1 <= attempt <= 16:
        return None
    if provider == "bcb_sgs":
        code, start, end = data.get("series_code"), data.get("requested_start"), data.get("requested_end")
        if type(code) is int and code > 0 and isinstance(start, str) and isinstance(end, str) and _valid_iso_date(start) and _valid_iso_date(end) and start <= end:
            if data.get("expected_kind") not in (None, "list"):
                return None
            return {"provider": provider, "series_code": code, "requested_start": start, "requested_end": end, "expected_kind": "list", "local_attempt": attempt}
    if provider == "b3":
        trade_date, filename = data.get("trade_date"), data.get("file_name")
        if isinstance(trade_date, str) and _valid_iso_date(trade_date) and isinstance(filename, str):
            day = date.fromisoformat(trade_date)
            expected = f"SPRD{day:%y%m%d}.zip"
            if filename == expected:
                return {"provider": provider, "trade_date": trade_date, "file_name": expected, "local_attempt": attempt}
    return None


def _type_name(error: BaseException) -> str:
    code = _known_code(error)
    if code is None:
        return "UnknownException"
    return {
        "dns_resolution_error": "gaierror", "tls_error": "SSLError",
    }.get(code, type(error).__name__ if type(error).__name__ in _SAFE_TYPES else "UnknownException")


def _known_code(error: BaseException) -> str | None:
    if isinstance(error, httpx.ConnectTimeout): return "connect_timeout"
    if isinstance(error, httpx.ReadTimeout): return "read_timeout"
    if isinstance(error, httpx.WriteTimeout): return "write_timeout"
    if isinstance(error, httpx.PoolTimeout): return "pool_timeout"
    if isinstance(error, httpx.ConnectError): return "connect_error"
    if isinstance(error, httpx.ReadError): return "read_error"
    if isinstance(error, httpx.WriteError): return "write_error"
    if isinstance(error, httpx.RemoteProtocolError): return "protocol_error"
    if isinstance(error, httpx.DecodingError): return "content_decoding_error"
    if isinstance(error, httpx.HTTPStatusError): return "http_status_error"
    if isinstance(error, socket.gaierror): return "dns_resolution_error"
    if isinstance(error, ssl.SSLError): return "tls_error"
    if isinstance(error, TimeoutError): return "timeout"
    if isinstance(error, ConnectionResetError): return "connection_reset"
    if isinstance(error, RuntimeError): return "unknown_runtime_error"
    name = type(error).__name__
    if name in {"JSONDecodeError", "InvalidSGSPayloadError"}:
        return _SAFE_TYPES[name]
    return None


def causal_chain(error: BaseException) -> list[dict[str, str]]:
    """Return at most four allowlisted exception types/codes, cycle-safe."""
    result: list[dict[str, str]] = []
    seen: set[int] = set()
    current: BaseException | None = error
    while current is not None and len(result) < MAX_CAUSES:
        identity = id(current)
        if identity in seen:
            result.append({"type": "Truncated", "code": "cause_cycle"})
            break
        seen.add(identity)
        code = _known_code(current) or "unknown_exception"
        name = _type_name(current)
        result.append({"type": name, "code": code})
        cause = current.__cause__
        if cause is None and not current.__suppress_context__:
            cause = current.__context__
        current = cause
    if current is not None and len(result) == MAX_CAUSES:
        if id(current) in seen:
            result[-1] = {"type": "Truncated", "code": "cause_cycle"}
        else:
            result[-1] = {"type": "Truncated", "code": "cause_limit"}
    return result


def classify_diagnostic(error: BaseException) -> str:
    """Classify observed failure evidence independently of operational policy."""
    chain = causal_chain(error)
    # Prefer the nearest known failure; an unknown deeper cause is not proof
    # that the observed outer timeout was not the best available evidence.
    root = next((cause["code"] for cause in reversed(chain) if cause["code"] not in {"unknown_exception", "unknown_runtime_error", "cause_cycle", "cause_limit"}), "unknown")
    if root in {"connect_timeout", "read_timeout", "write_timeout", "pool_timeout", "timeout"}:
        return root
    if root in {"dns_resolution_error", "tls_error", "connect_error", "read_error", "write_error", "connection_reset"}:
        return root
    if root in {"invalid_json", "invalid_json_shape", "protocol_error", "content_decoding_error", "http_status_error"}:
        return root
    if isinstance(error, httpx.HTTPStatusError):
        return "http_status_error"
    return "unknown"


def response_evidence(body: bytes | None, content_type: str | None, status: int | None) -> dict[str, object]:
    """Summarize already-read bytes; never retain body content."""
    normalized = content_type.split(";", 1)[0].strip().lower() if content_type and len(content_type) <= 128 else "unknown"
    if normalized not in _MIME_TYPES:
        normalized = "unknown"
    result: dict[str, object] = {
        "status_code": status if isinstance(status, int) and not isinstance(status, bool) and 100 <= status <= 599 else None,
        "content_type": normalized,
    }
    if body is None or len(body) > MAX_BODY_BYTES:
        result["body_kind"] = "unknown"
        return result
    result.update({"bytes_received": len(body), "body_sha256": hashlib.sha256(body).hexdigest()})
    try:
        value = json.loads(body)
    except (ValueError, UnicodeDecodeError, RecursionError):
        result["body_kind"] = "invalid_json"
        return result
    if value is None:
        kind = "null"
    elif isinstance(value, bool):
        kind = "boolean"
    elif isinstance(value, dict):
        kind = "object"
    elif isinstance(value, list):
        kind = "list"
    elif isinstance(value, str):
        kind = "string"
    elif isinstance(value, int | float) and (not isinstance(value, float) or math.isfinite(value)):
        kind = "number"
    else:
        kind = "unknown"
    result["body_kind"] = kind
    if isinstance(value, dict):
        keys: list[str] = []
        for raw_key in cast(dict[object, object], value).keys():
            if isinstance(raw_key, str):
                keys.append(raw_key.lower())
        result["envelope_keys"] = sorted(key for key in keys if key in _ENVELOPE_KEYS)[:9]
        result["unknown_key_count"] = sum(key not in _ENVELOPE_KEYS for key in keys)
    return result


def build_diagnostic(
    *, error: BaseException | None = None, body: bytes | None = None,
    content_type: str | None = None, status: int | None = None,
    phase: str = "request", duration_ms: float | None = None,
) -> dict[str, object]:
    """Construct an allowlisted request diagnostic from observed evidence."""
    result: dict[str, object] = {"version": DIAGNOSTICS_VERSION, "phase": phase if phase in _PHASES else "request"}
    if duration_ms is not None and math.isfinite(duration_ms) and duration_ms >= 0:
        result["duration_ms"] = round(duration_ms, 3)
    if error is not None:
        chain = causal_chain(error)
        result["failure_code"] = classify_diagnostic(error)
        result["causes"] = chain
    result["response"] = response_evidence(body, content_type, status)
    return result


def bounded_diagnostic(value: Mapping[str, object]) -> dict[str, object]:
    """Reconstruct a strict safe schema; unknown keys and values are discarded."""
    try:
        safe_value: dict[str, object] = {"version": DIAGNOSTICS_VERSION}
        phase = value.get("phase")
        safe_value["phase"] = phase if isinstance(phase, str) and phase in _PHASES else "request"
        failure = value.get("failure_code")
        safe_failures = frozenset({"connect_timeout", "read_timeout", "write_timeout", "pool_timeout", "timeout", "dns_resolution_error", "tls_error", "connect_error", "read_error", "write_error", "connection_reset", "invalid_json", "invalid_json_shape", "protocol_error", "content_decoding_error", "http_status_error", "unknown", "diagnostics_truncated", "diagnostics_unavailable"})
        if isinstance(failure, str) and failure in safe_failures:
            safe_value["failure_code"] = failure
        duration = value.get("duration_ms")
        if isinstance(duration, (int, float)) and not isinstance(duration, bool) and math.isfinite(duration) and 0 <= duration <= 86400000:
            safe_value["duration_ms"] = round(duration, 3)
        response = value.get("response")
        if isinstance(response, Mapping):
            response = cast(Mapping[str, object], response)
            status = response.get("status_code")
            mime = response.get("content_type")
            kind = response.get("body_kind")
            clean: dict[str, object] = {
                "status_code": status if isinstance(status, int) and not isinstance(status, bool) and 100 <= status <= 599 else None,
                "content_type": mime if isinstance(mime, str) and mime in _MIME_TYPES else "unknown",
                "body_kind": kind if isinstance(kind, str) and kind in {"unknown", "invalid_json", "null", "boolean", "object", "list", "string", "number"} else "unknown",
            }
            count = response.get("bytes_received")
            if isinstance(count, int) and not isinstance(count, bool) and 0 <= count <= MAX_BODY_BYTES:
                clean["bytes_received"] = count
            digest = response.get("body_sha256")
            if isinstance(digest, str) and len(digest) == 64 and all(c in "0123456789abcdef" for c in digest):
                clean["body_sha256"] = digest
            raw_keys = response.get("envelope_keys")
            if isinstance(raw_keys, list):
                typed_keys = cast(list[object], raw_keys)
                clean["envelope_keys"] = sorted({key for key in typed_keys if isinstance(key, str) and key in _ENVELOPE_KEYS})[:9]
            unknown_count = response.get("unknown_key_count")
            if isinstance(unknown_count, int) and not isinstance(unknown_count, bool) and 0 <= unknown_count <= 100000:
                clean["unknown_key_count"] = unknown_count
            safe_value["response"] = clean
        causes = value.get("causes")
        if isinstance(causes, list):
            clean_causes: list[dict[str, str]] = []
            allowed_codes = frozenset(_SAFE_TYPES.values()) | {"unknown_exception", "cause_cycle", "cause_limit"}
            typed_causes = cast(list[object], causes)
            for cause_value in typed_causes[:MAX_CAUSES]:
                cause = cause_value
                if isinstance(cause, Mapping):
                    cause = cast(Mapping[str, object], cause)
                    code = cause.get("code")
                    type_name = cause.get("type")
                    valid_pairs = {**_SAFE_TYPES, "UnknownException": "unknown_exception", "Truncated": "cause_cycle"}
                    if code == "cause_limit":
                        valid_pairs["Truncated"] = "cause_limit"
                    if isinstance(code, str) and isinstance(type_name, str) and code in allowed_codes and valid_pairs.get(type_name) == code:
                        clean_causes.append({"type": type_name, "code": code})
            safe_value["causes"] = clean_causes
        context = _sanitize_request_context(value.get("request_context"))
        if context is not None:
            safe_value["request_context"] = context
        encoded = json.dumps(safe_value, allow_nan=False, separators=(",", ":")).encode()
        if len(encoded) > 2048:
            return {"version": DIAGNOSTICS_VERSION, "failure_code": "diagnostics_truncated"}
        return safe_value
    except Exception:
        return {"version": DIAGNOSTICS_VERSION, "failure_code": "diagnostics_unavailable"}
