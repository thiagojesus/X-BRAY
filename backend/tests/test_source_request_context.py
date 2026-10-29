from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from contextvars import copy_context
from datetime import date
from json import dumps
from threading import Lock

import httpx

from datafetchers import b3_di
from ingestion_policy import (
    ErrorCategory,
    RetryBudget,
    RetryProfile,
    classify_exception,
    install_retry_budget,
    reset_retry_budget,
)
from source_diagnostics import current_request_context, source_request_context


def test_b3_parallel_local_attempts_keep_trade_date_and_file_identity(monkeypatch) -> None:
    attempts: dict[str, int] = {}
    attempts_lock = Lock()

    def fake_get(url: str, **_kwargs):
        with attempts_lock:
            attempts[url] = attempts.get(url, 0) + 1
            count = attempts[url]
        if "SPRD261028.zip" in url and count == 1:
            raise httpx.ReadTimeout("private read-timeout detail")
        return httpx.Response(200, content=b"synthetic-zip", request=httpx.Request("GET", url))

    monkeypatch.setattr(httpx, "get", fake_get)
    monkeypatch.setattr(b3_di, "_parse_spr", lambda _body: [])
    monkeypatch.setattr(b3_di.time, "sleep", lambda _delay: None)
    budget = RetryBudget(RetryProfile("b3", 64, 240, 30))
    token = install_retry_budget(budget)
    days = [date(2026, 10, 28), date(2026, 10, 29)]
    try:
        with ThreadPoolExecutor(max_workers=2) as executor:
            contexts = [copy_context() for _ in days]
            results = list(executor.map(lambda pair: pair[0].run(b3_di._download_day_untracked, pair[1]), zip(contexts, days)))
    finally:
        reset_retry_budget(token)

    assert results == [[], []]
    assert budget.requests == 3
    assert budget.retries == 0
    by_date: dict[str, list[dict[str, object]]] = {}
    for local_attempt, trace in enumerate(budget.http_trace, start=1):
        diagnostics = trace["diagnostics"]
        assert isinstance(diagnostics, dict)
        context = diagnostics["request_context"]
        assert isinstance(context, dict)
        by_date.setdefault(str(context["trade_date"]), []).append(context)
    assert by_date["2026-10-28"] == [
        {"provider": "b3", "trade_date": "2026-10-28", "file_name": "SPRD261028.zip", "local_attempt": 1},
        {"provider": "b3", "trade_date": "2026-10-28", "file_name": "SPRD261028.zip", "local_attempt": 2},
    ]
    assert by_date["2026-10-29"] == [
        {"provider": "b3", "trade_date": "2026-10-29", "file_name": "SPRD261029.zip", "local_attempt": 1},
    ]
    failed = next(trace for trace in budget.http_trace if trace["status_code"] is None)
    failure_diag = failed["diagnostics"]
    assert isinstance(failure_diag, dict)
    assert failure_diag["failure_code"] == "read_timeout"
    assert "private read-timeout detail" not in dumps(budget.http_trace)
    assert current_request_context() is None


def test_request_identity_validation_and_nested_context_restore() -> None:
    assert current_request_context() is None
    with source_request_context(provider="bcb_sgs", series_code=433, requested_start="2007-03-01", requested_end="2007-03-31") as outer:
        assert outer is not None
        assert current_request_context() == outer
        with source_request_context(provider="b3", trade_date="2026-10-29", file_name="SPRD261029.zip", local_attempt=2) as inner:
            assert inner is not None
            assert current_request_context() == inner
        assert current_request_context() == outer
        with source_request_context(provider="b3", trade_date="2026-10-29", file_name="../../SPRD261029.zip", local_attempt=True) as invalid_nested:
            assert invalid_nested is None
            assert current_request_context() is None
        assert current_request_context() == outer
    assert current_request_context() is None
    with source_request_context(provider="b3", trade_date="2026-10-29", file_name="../../SPRD261029.zip", local_attempt=True) as invalid:
        assert invalid is None
        assert current_request_context() is None
    with source_request_context(provider="legacy", series_code=433) as unknown:
        assert unknown is None
        assert current_request_context() is None
    try:
        with source_request_context(provider="b3", trade_date="2026-10-29", file_name="SPRD261029.zip"):
            raise RuntimeError("synthetic failure")
    except RuntimeError:
        pass
    assert current_request_context() is None


def test_b3_wrapped_retry_failure_keeps_diagnostic_root_without_policy_reclassification(monkeypatch) -> None:
    def timed_out(url: str, **_kwargs):
        raise httpx.ReadTimeout("private transport detail", request=httpx.Request("GET", url))

    monkeypatch.setattr(httpx, "get", timed_out)
    monkeypatch.setattr(b3_di.time, "sleep", lambda _delay: None)
    budget = RetryBudget(RetryProfile("b3", 64, 240, 30))
    token = install_retry_budget(budget)
    try:
        try:
            b3_di._download_day_untracked(date(2026, 10, 28))
        except RuntimeError as error:
            assert classify_exception(error) == ErrorCategory.INTERNAL_ERROR
        else:
            raise AssertionError("expected local B3 retry exhaustion")
    finally:
        reset_retry_budget(token)
    assert len(budget.http_trace) == 2
    for local_attempt, trace in enumerate(budget.http_trace, start=1):
        diagnostics = trace["diagnostics"]
        assert isinstance(diagnostics, dict)
        assert diagnostics["failure_code"] == "read_timeout"
        assert diagnostics["request_context"] == {
            "provider": "b3", "trade_date": "2026-10-28",
            "file_name": "SPRD261028.zip", "local_attempt": local_attempt,
        }
