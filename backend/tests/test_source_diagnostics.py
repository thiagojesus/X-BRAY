from __future__ import annotations

import json
import socket
import ssl
from concurrent.futures import ThreadPoolExecutor

import httpx

from ingestion_policy import (
    ErrorCategory,
    RetryBudget,
    RetryProfile,
    classify_exception,
)
from source_diagnostics import (
    bounded_diagnostic,
    causal_chain,
    classify_diagnostic,
    response_evidence,
)


def test_response_evidence_never_contains_body_values() -> None:
    body = json.dumps({"message": "Bearer secret-token", "points": [1, 2]}).encode()
    evidence = response_evidence(body, "Application/JSON; charset=utf-8", 200)
    serialized = json.dumps(evidence)
    assert evidence["body_kind"] == "object"
    assert evidence["content_type"] == "application/json"
    assert evidence["unknown_key_count"] == 1
    assert "secret-token" not in serialized
    assert "points" not in serialized


def test_response_evidence_invalid_and_oversized_body() -> None:
    assert response_evidence(b"<script>secret</script>", "text/html", 200)["body_kind"] == "invalid_json"
    evidence = response_evidence(b"x" * (10 * 1024 * 1024 + 1), None, 200)
    assert evidence["body_kind"] == "unknown"
    assert "body_sha256" not in evidence


def test_causal_chain_preserves_outer_and_known_root_without_messages() -> None:
    try:
        raise httpx.ReadTimeout("password=secret")
    except httpx.ReadTimeout as root:
        outer = RuntimeError("private URL https://example.invalid/token")
        outer.__cause__ = root
    chain = causal_chain(outer)
    assert chain == [
        {"type": "RuntimeError", "code": "unknown_runtime_error"},
        {"type": "ReadTimeout", "code": "read_timeout"},
    ]
    assert classify_diagnostic(outer) == "read_timeout"
    assert "secret" not in repr(chain)


def test_unknown_exception_is_safe() -> None:
    class SecretFailure(Exception):
        pass

    err = SecretFailure("cpf=123")
    assert causal_chain(err) == [{"type": "UnknownException", "code": "unknown_exception"}]
    assert classify_diagnostic(err) == "unknown"


def test_diagnostic_reconstruction_discards_nested_untrusted_values() -> None:
    secret = "Bearer sentinel-password@example.invalid"
    incoming = {
        "version": secret, "phase": secret, "request_url": secret,
        "failure_code": secret,
        "response": {
            "status_code": True, "content_type": secret, "body_kind": "object",
            "envelope_keys": [secret, "message"], "unknown_key_count": 1,
            "body": secret,
        },
        "causes": [{"type": secret, "code": secret, "message": secret}],
        "request_context": {
            "provider": "b3", "trade_date": "2026-10-29",
            "file_name": "../../SPRD261029.zip", "local_attempt": 1,
            "headers": {"authorization": secret}, "claim_token": secret,
        },
    }
    rendered = json.dumps(bounded_diagnostic(incoming))
    assert secret not in rendered
    assert "request_url" not in rendered
    assert "sentinel-password" not in rendered


def test_sanitizer_rejects_inconsistent_cause_pairs_and_invalid_scalars() -> None:
    safe = bounded_diagnostic({
        "phase": "response_read", "duration_ms": float("nan"),
        "response": {"status_code": True, "content_type": "application/json", "body_kind": "object",
                     "bytes_received": -1, "body_sha256": "z" * 64},
        "causes": [{"type": "ReadTimeout", "code": "tls_error"}],
    })
    assert "duration_ms" not in safe
    response = safe["response"]
    assert isinstance(response, dict)
    assert response["status_code"] is None
    assert "bytes_received" not in response
    assert "body_sha256" not in response
    assert safe["causes"] == []


def test_unknown_mime_and_invalid_numeric_metadata_are_safe() -> None:
    secret = "postgres://user:password@host/db"
    evidence = response_evidence(b'{"a": NaN}', secret, True)
    assert evidence["content_type"] == "unknown"
    assert secret not in json.dumps(evidence)


def test_native_known_causes_and_context_rules() -> None:
    errors = [httpx.ConnectTimeout("x"), httpx.ReadTimeout("x"), httpx.WriteTimeout("x"), httpx.PoolTimeout("x")]
    assert [classify_diagnostic(error) for error in errors] == ["connect_timeout", "read_timeout", "write_timeout", "pool_timeout"]
    assert classify_diagnostic(httpx.ConnectError("irrelevant")) == "connect_error"
    assert classify_diagnostic(socket.gaierror("sensitive dns")) == "dns_resolution_error"
    assert classify_diagnostic(ssl.SSLError("sensitive tls")) == "tls_error"
    wrapped_connect = httpx.ConnectError("generic connection")
    wrapped_connect.__cause__ = ssl.SSLError("specific TLS cause")
    assert classify_diagnostic(wrapped_connect) == "tls_error"
    request = httpx.Request("GET", "https://fixture.example.invalid")
    response = httpx.Response(503, request=request)
    assert classify_diagnostic(httpx.HTTPStatusError("private", request=request, response=response)) == "http_status_error"
    assert classify_diagnostic(httpx.RemoteProtocolError("private")) == "protocol_error"
    assert classify_diagnostic(httpx.DecodingError("private")) == "content_decoding_error"
    try:
        raise httpx.ReadTimeout("private timeout")
    except httpx.ReadTimeout as inner:
        outer = RuntimeError("private wrapper")
        outer.__cause__ = inner
    assert classify_diagnostic(outer) == "read_timeout"
    assert classify_diagnostic(RuntimeError("outer")) == "unknown"


def test_causal_chain_cycle_suppression_and_limit_are_bounded() -> None:
    first, second = RuntimeError("a"), RuntimeError("b")
    first.__cause__ = second
    second.__cause__ = first
    cycle = causal_chain(first)
    assert cycle[-1] == {"type": "Truncated", "code": "cause_cycle"}
    root = RuntimeError("root")
    root.__context__ = httpx.ReadTimeout("suppressed")
    root.__suppress_context__ = True
    assert causal_chain(root) == [{"type": "RuntimeError", "code": "unknown_runtime_error"}]
    cursor = root
    for _ in range(6):
        nested = RuntimeError("nested")
        cursor.__cause__ = nested
        cursor = nested
    assert causal_chain(root)[-1] == {"type": "Truncated", "code": "cause_limit"}


def test_deep_json_recursion_and_oversized_body_fail_closed(monkeypatch) -> None:
    monkeypatch.setattr("source_diagnostics.json.loads", lambda _body: (_ for _ in ()).throw(RecursionError()))
    result = response_evidence(b'{"message":"secret"}', "application/json", 200)
    assert result["body_kind"] == "invalid_json"
    assert b"secret" not in json.dumps(result).encode()
    assert response_evidence(b"x" * (10 * 1024 * 1024 + 1), None, 200)["body_kind"] == "unknown"


def test_parallel_trace_is_bounded_unique_and_has_no_cross_contamination() -> None:
    budget = RetryBudget(RetryProfile("bcb_sgs", 3, 20, 2))

    def record(index: int) -> None:
        budget.record_http_trace(
            route_id=f"route_{index}", allowed_host="api.example.invalid",
            attempt_ordinal=1, hop_ordinal=1, status_code=200,
            dispatch_started_at="2026-10-29T00:00:00+00:00",
            dispatch_finished_at="2026-10-29T00:00:01+00:00",
            bytes_received=0, dispatch_kind="initial",
            diagnostic={"phase": "response_read", "response": {"body_kind": "null"}, "private": f"Bearer sentinel-{index}"},
        )

    with ThreadPoolExecutor(max_workers=8) as executor:
        list(executor.map(record, range(400)))
    assert len(budget.http_trace) == 256
    sequences = [entry["dispatch_sequence"] for entry in budget.http_trace]
    assert len(set(sequences)) == 256
    encoded = json.dumps(budget.http_trace)
    assert "Bearer" not in encoded
    assert len(encoded.encode()) <= 512 * 1024


def test_diagnostic_timeout_does_not_change_operational_wrapped_error_category() -> None:
    try:
        raise httpx.ReadTimeout("secret")
    except httpx.ReadTimeout as cause:
        wrapped = RuntimeError("private wrapper")
        wrapped.__cause__ = cause
    assert classify_diagnostic(wrapped) == "read_timeout"
    assert classify_exception(wrapped) == ErrorCategory.INTERNAL_ERROR
