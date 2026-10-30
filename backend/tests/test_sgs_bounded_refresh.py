import json
from collections.abc import Callable
from datetime import datetime, timezone
from json import loads
from time import monotonic
from typing import ClassVar
from unittest.mock import Mock

import httpx
import pytest

pytestmark = pytest.mark.usefixtures("isolated_sqlite_store")

from datafetchers import bcb_sgs
from datafetchers.bcb_sgs import InvalidSGSPayloadError
from ingestion_policy import (
    RetryBudget,
    RetryProfile,
    install_retry_budget,
    reset_retry_budget,
)
from provider_http import ProviderInvalidJSONResponseError
from refresh import _error_code


class FakeResponse:
    def __init__(self, payload: object) -> None:
        self.payload = payload
        self.status_code = 200
        self.headers = {"content-type": "application/json"}
        self.content = json.dumps(payload).encode()

    def json(self) -> object:
        return self.payload


class InvalidJsonResponse:
    status_code = 200
    headers: ClassVar[dict[str, str]] = {"content-type": "text/html; charset=utf-8"}
    content = b"<html>provider temporarily rejected the request</html>"

    def json(self) -> object:
        raise json.JSONDecodeError("invalid response", self.content.decode(), 0)


def _prepare_fetch(
    monkeypatch: pytest.MonkeyPatch,
    response_factory: Callable[[], FakeResponse],
) -> tuple[Mock, Mock, Mock]:
    request = Mock(side_effect=lambda *_args, **_kwargs: response_factory())
    upsert = Mock()
    metadata = Mock()
    monkeypatch.setattr(bcb_sgs, "get_sgs_range", lambda _code: (None, None))
    monkeypatch.setattr(bcb_sgs, "get_provider_response", request)
    monkeypatch.setattr(bcb_sgs, "upsert_sgs", upsert)
    monkeypatch.setattr(bcb_sgs, "set_meta", metadata)
    monkeypatch.setattr(bcb_sgs, "query_sgs", lambda *_args: [])
    return request, upsert, metadata


def test_sgs_refresh_plan_is_four_independent_ten_year_windows(monkeypatch) -> None:
    monkeypatch.setattr(bcb_sgs, "datetime", type("FrozenDateTime", (), {"now": staticmethod(lambda _tz: datetime(2026, 10, 25, tzinfo=timezone.utc))}))
    assert bcb_sgs.sgs_refresh_windows() == (
        ("01/01/1990", "01/01/2000"),
        ("01/01/2000", "01/01/2010"),
        ("01/01/2010", "01/01/2020"),
            ("01/01/2020", "25/10/2026"),
    )


def test_sgs_annual_windows_accept_iso_and_legacy_inclusive_bounds() -> None:
    assert bcb_sgs.sgs_annual_windows("2015-01-01", "2017-02-03") == (
        ("01/01/2015", "31/12/2015"),
        ("01/01/2016", "31/12/2016"),
        ("01/01/2017", "03/02/2017"),
    )
    assert bcb_sgs.sgs_annual_windows("01/01/2020", "31/12/2020") == (
        ("01/01/2020", "31/12/2020"),
    )
    with pytest.raises(ValueError):
        bcb_sgs.sgs_annual_windows("2021-01-01", "2020-12-31")


def test_annual_window_source_fetches_and_records_actual_unique_counts(monkeypatch) -> None:
    payload = [
        {"data": "01/01/2015", "dataFim": "01/02/2015", "valor": "1.0"},
        {"data": "01/01/2015", "dataFim": "02/02/2015", "valor": "1.00"},
        {"data": "02/01/2015", "dataFim": "02/02/2015", "valor": "0"},
    ]
    monkeypatch.setattr(bcb_sgs, "query_sgs", lambda *_args: [])
    # Series 226 returns the canonical period reader rather than query_sgs;
    # isolate that read too so this provider-payload unit test cannot observe
    # rows from a developer's persistent SQLite database.
    monkeypatch.setattr(bcb_sgs, "query_tr_periods", lambda *_args: [])
    monkeypatch.setattr(bcb_sgs, "_window_cache_fresh", lambda *_args: False)
    monkeypatch.setattr(bcb_sgs, "get_sgs_range", lambda _code: (None, None))
    monkeypatch.setattr(bcb_sgs, "upsert_tr_periods", Mock(return_value=3))
    monkeypatch.setattr(bcb_sgs, "set_meta", Mock())
    bounded = Mock(return_value=FakeResponse(payload))
    monkeypatch.setattr(bcb_sgs, "get_provider_response_bounded", bounded)

    result = bcb_sgs.fetch_sgs_window(
        226, start_date="01/01/2015", end_date="31/12/2015",
        use_cache=False, fail_fast=True, request_timeout=20,
    )

    assert result == payload
    assert bounded.call_args.kwargs["deadline_seconds"] <= 20
    bcb_sgs.upsert_tr_periods.assert_called_once()
    assert bcb_sgs.upsert_tr_periods.call_args.args == (payload,)
    assert bcb_sgs.upsert_tr_periods.call_args.kwargs["deadline_at"] > monotonic()


def test_tr_window_producer_records_explicit_contract_metadata_in_sqlite_ledger(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from db.store import query_tr_periods, source_extractions_for_refresh_run
    from ingestion_policy import current_retry_budget
    from source_ingestion import reset_refresh_context, set_refresh_context

    payload = [{"data": "01/03/2015", "dataFim": "31/03/2015", "valor": "0.0661"}]
    monkeypatch.setattr(bcb_sgs, "_window_cache_fresh", lambda *_args: False)

    def mocked_http(*_args: object, **_kwargs: object) -> FakeResponse:
        budget = current_retry_budget()
        assert budget is not None
        budget.reserve_request()
        return FakeResponse(payload)

    monkeypatch.setattr(bcb_sgs, "get_provider_response_bounded", mocked_http)
    run_id = "sgs-contract-metadata-producer-test"
    task_key = "sgs_juros_w226_2015-01-01_2015-12-31"
    context_token = set_refresh_context(run_id, task_key, "cycle-contract-metadata")
    try:
        result = bcb_sgs.fetch_sgs_window(
            226,
            start_date="01/01/2015",
            end_date="31/12/2015",
            use_cache=False,
            fail_fast=True,
            request_timeout=20,
        )
    finally:
        reset_refresh_context(context_token)

    extraction = next(
        row
        for row in source_extractions_for_refresh_run(run_id)
        if row["task_key"] == task_key
    )
    qualifiers = loads(str(extraction["qualifiers_json"]))
    stored_period = query_tr_periods("2015-01-01", "2015-12-31")
    assert result == payload
    assert extraction["status"] == "success"
    assert extraction["source"] == "bcb_sgs"
    assert extraction["indicator"] == "226"
    assert extraction["network_called"] == 1
    assert extraction["request_count"] == 1
    assert extraction["received_count"] == 1
    assert extraction["valid_count"] == 1
    assert extraction["persisted_count"] == 1
    assert qualifiers == {
        "contract_version": "acquisition-contract-v1",
        "end_date": "31/12/2015",
        "identity_kind": "full_period",
        "series_code": 226,
        "start_date": "01/01/2015",
        "unit": "pct_period",
        "window_policy": "calendar-year-v1",
    }
    assert stored_period[0]["data"] == "01/03/2015"
    assert stored_period[0]["dataFim"] == "31/03/2015"


def test_annual_window_source_rejects_conflicting_duplicates_without_store(monkeypatch) -> None:
    payload = [
        {"data": "01/01/2015", "dataFim": "01/02/2015", "valor": "1.0"},
        {"data": "01/01/2015", "dataFim": "01/02/2015", "valor": "2.0"},
    ]
    monkeypatch.setattr(bcb_sgs, "query_sgs", lambda *_args: [])
    monkeypatch.setattr(bcb_sgs, "_window_cache_fresh", lambda *_args: False)
    monkeypatch.setattr(bcb_sgs, "get_sgs_range", lambda _code: (None, None))
    upsert = Mock()
    monkeypatch.setattr(bcb_sgs, "upsert_tr_periods", upsert)
    monkeypatch.setattr(bcb_sgs, "set_meta", Mock())
    monkeypatch.setattr(bcb_sgs, "get_provider_response_bounded", Mock(return_value=FakeResponse(payload)))

    with pytest.raises(InvalidSGSPayloadError, match="conflicting-duplicate-period"):
        bcb_sgs.fetch_sgs_window(
            226, start_date="01/01/2015", end_date="31/12/2015",
            use_cache=False, fail_fast=True, request_timeout=20,
        )
    upsert.assert_not_called()


def test_tr_normalization_preserves_same_start_distinct_periods() -> None:
    rows = [
        {"data": "01/03/2015", "dataFim": "29/03/2015", "valor": "0.0661"},
        {"data": "01/03/2015", "dataFim": "30/03/2015", "valor": "0.0661"},
        {"data": "01/03/2015", "dataFim": "31/03/2015", "valor": "0.0978"},
    ]
    assert bcb_sgs._normalize_window_records(rows, 226, "01/01/2015", "31/12/2015") == rows


def test_tr_normalization_rejects_missing_end_and_true_duplicate_conflict() -> None:
    with pytest.raises(InvalidSGSPayloadError, match="invalid-period-end"):
        bcb_sgs._normalize_window_records([{"data": "01/03/2015", "valor": "0.1"}], 226, None, None)
    conflict = [
        {"data": "01/03/2015", "dataFim": "31/03/2015", "valor": "0.1"},
        {"data": "01/03/2015", "dataFim": "31/03/2015", "valor": "0.2"},
    ]
    with pytest.raises(InvalidSGSPayloadError, match="conflicting-duplicate-period"):
        bcb_sgs._normalize_window_records(conflict, 226, None, None)


def test_bounded_window_uses_supplied_timeout_and_defers_freshness(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    request, upsert, metadata = _prepare_fetch(
        monkeypatch,
        lambda: FakeResponse([{"data": "01/01/2026", "valor": "1.2"}]),
    )
    bounded = Mock(return_value=FakeResponse([{"data": "01/01/2026", "valor": "1.2"}]))
    monkeypatch.setattr(bcb_sgs, "get_provider_response_bounded", bounded)

    rows = bcb_sgs.fetch_sgs_series(
        1635,
        start_date="01/01/2020",
        end_date="31/12/2026",
        use_cache=False,
        mark_refreshed=False,
        fail_fast=True,
        request_timeout=5,
    )

    assert rows == [{"data": "01/01/2026", "valor": "1.2"}]
    bounded.assert_called_once()
    assert bounded.call_args.kwargs["deadline_seconds"] <= 5
    assert bounded.call_args.kwargs["max_bytes"] == 256 * 1024
    request.assert_not_called()
    upsert.assert_called_once()
    assert upsert.call_args.args == (1635, rows)
    assert upsert.call_args.kwargs["deadline_at"] > monotonic()
    metadata.assert_not_called()


def test_bounded_window_propagates_provider_failure_without_marking_fresh(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    request = Mock()
    bounded = Mock(side_effect=TimeoutError("provider timeout"))
    metadata = Mock()
    monkeypatch.setattr(bcb_sgs, "get_sgs_range", lambda _code: (None, None))
    monkeypatch.setattr(bcb_sgs, "get_provider_response", request)
    monkeypatch.setattr(bcb_sgs, "get_provider_response_bounded", bounded)
    monkeypatch.setattr(bcb_sgs, "query_sgs", lambda *_args: [])
    monkeypatch.setattr(bcb_sgs, "set_meta", metadata)

    with pytest.raises(TimeoutError, match="provider timeout"):
        bcb_sgs.fetch_sgs_series(
            1635,
            start_date="01/01/2020",
            end_date="31/12/2026",
            use_cache=False,
            mark_refreshed=False,
            fail_fast=True,
            request_timeout=5,
        )

    metadata.assert_not_called()
    request.assert_not_called()


def test_bounded_sgs_retries_invalid_html_then_accepts_valid_json(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _, upsert, metadata = _prepare_fetch(
        monkeypatch,
        lambda: FakeResponse([{"data": "01/01/2026", "valor": "1.2"}]),
    )
    bounded = Mock(
        side_effect=[
            InvalidJsonResponse(),
            FakeResponse([{"data": "01/01/2026", "valor": "1.2"}]),
        ]
    )
    monkeypatch.setattr(bcb_sgs, "get_provider_response_bounded", bounded)
    wait = Mock()
    monkeypatch.setattr(bcb_sgs, "sleep", wait)

    rows = bcb_sgs.fetch_sgs_series(
        1635,
        start_date="01/01/2020",
        end_date="31/12/2026",
        use_cache=False,
        mark_refreshed=False,
        fail_fast=True,
        request_timeout=5,
    )

    assert rows == [{"data": "01/01/2026", "valor": "1.2"}]
    assert bounded.call_count == 2
    assert bounded.call_args_list[0].kwargs["deadline_seconds"] <= 5
    assert bounded.call_args_list[1].kwargs["deadline_seconds"] < bounded.call_args_list[0].kwargs["deadline_seconds"]
    wait.assert_called_once_with(0.25)
    upsert.assert_called_once()
    assert upsert.call_args.args == (1635, rows)
    assert upsert.call_args.kwargs["deadline_at"] > monotonic()
    metadata.assert_not_called()


def test_v6_sgs_complete_invalid_json_is_terminal_without_body_retry(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    bounded = Mock(return_value=InvalidJsonResponse())
    monkeypatch.setattr(bcb_sgs, "get_provider_response_bounded", bounded)
    monkeypatch.setattr(bcb_sgs, "_is_v6_sgs_plan_claim", lambda: True)
    with pytest.raises(ProviderInvalidJSONResponseError):
        bcb_sgs._fetch_bounded_sgs_json("https://fixture.invalid/sgs", {}, 5)
    bounded.assert_called_once()


@pytest.mark.parametrize("status_code", [400, 401, 403, 404, 410, 422])
@pytest.mark.parametrize("fetcher", ["bounded", "legacy"])
def test_sgs_permanent_http_status_is_terminal_without_retry_or_sleep(
    monkeypatch: pytest.MonkeyPatch, status_code: int, fetcher: str,
) -> None:
    response = httpx.Response(status_code, request=httpx.Request("GET", "https://fixture.invalid/sgs"))
    error = httpx.HTTPStatusError("permanent response", request=response.request, response=response)
    request = Mock(side_effect=error)
    wait = Mock()
    monkeypatch.setattr(bcb_sgs, "sleep", wait)
    if fetcher == "bounded":
        monkeypatch.setattr(bcb_sgs, "get_provider_response_bounded", request)
        call = lambda: bcb_sgs._fetch_bounded_sgs_json("https://fixture.invalid/sgs", {}, 5)
    else:
        monkeypatch.setattr(bcb_sgs, "get_provider_response", request)
        call = lambda: bcb_sgs._fetch_sgs_json("https://fixture.invalid/sgs", {}, 5)

    with pytest.raises(httpx.HTTPStatusError) as raised:
        call()

    assert raised.value is error
    request.assert_called_once()
    wait.assert_not_called()


def test_observed_monthly_label_is_accepted_only_by_aligned_window() -> None:
    from datafetchers.bcb_sgs import _normalize_window_records, _validate_sgs_payload

    observed = [{"data": "01/10/2024", "valor": "0.61"}]
    validated = _validate_sgs_payload(observed, 188, "01/10/2024", "27/10/2026")
    assert _normalize_window_records(validated, 188, "01/10/2024", "27/10/2026") == observed
    with pytest.raises(InvalidSGSPayloadError) as raised:
        _normalize_window_records(
            _validate_sgs_payload(observed, 188, "27/10/2024", "27/10/2026"),
            188, "27/10/2024", "27/10/2026",
        )
    assert raised.value.reason == "outside-requested-window"


def test_monthly_inpc_observed_rows_pass_through_bounded_producer_when_window_aligned(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    raw_sample = [
        {"data": "01/10/2024", "valor": "0.61"},
        {"data": "01/11/2024", "valor": "0.33"},
    ]
    _prepare_fetch(monkeypatch, lambda: FakeResponse([]))
    bounded = Mock(return_value=FakeResponse(raw_sample))
    monkeypatch.setattr(bcb_sgs, "get_provider_response_bounded", bounded)

    result = bcb_sgs.fetch_sgs_series(
        188, start_date="01/10/2024", end_date="27/10/2026", use_cache=False,
        mark_refreshed=False, fail_fast=True, request_timeout=5,
    )

    assert result == raw_sample
    bounded.assert_called_once()
    assert bounded.call_args.kwargs["params"]["dataInicial"] == "01/10/2024"


def test_unaligned_legacy_monthly_window_rejects_observed_label_once_without_retry(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    raw_sample = [{"data": "01/10/2024", "valor": "0.61"}]
    _, upsert, metadata = _prepare_fetch(monkeypatch, lambda: FakeResponse([]))
    bounded = Mock(return_value=FakeResponse(raw_sample))
    monkeypatch.setattr(bcb_sgs, "get_provider_response_bounded", bounded)

    with pytest.raises(InvalidSGSPayloadError) as rejected:
        bcb_sgs.fetch_sgs_series(
            188, start_date="27/10/2024", end_date="27/10/2026", use_cache=False,
            mark_refreshed=False, fail_fast=True, request_timeout=5,
        )

    assert rejected.value.reason == "outside-requested-window"
    bounded.assert_called_once()
    upsert.assert_not_called()
    metadata.assert_not_called()


def test_bounded_sgs_exhausts_invalid_json_retries_with_safe_diagnostic(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _, upsert, metadata = _prepare_fetch(
        monkeypatch,
        lambda: FakeResponse([{"data": "01/01/2026", "valor": "1.2"}]),
    )
    bounded = Mock(side_effect=[InvalidJsonResponse() for _ in range(3)])
    monkeypatch.setattr(bcb_sgs, "get_provider_response_bounded", bounded)
    monkeypatch.setattr(bcb_sgs, "sleep", Mock())

    with pytest.raises(ProviderInvalidJSONResponseError) as error:
        bcb_sgs.fetch_sgs_series(
            1635,
            start_date="01/01/2020",
            end_date="31/12/2026",
            use_cache=False,
            mark_refreshed=False,
            fail_fast=True,
            request_timeout=5,
        )

    assert bounded.call_count == 3
    assert "HTTP 200" in str(error.value)
    assert "text/html; charset=utf-8" in str(error.value)
    assert "temporarily rejected" not in str(error.value)
    upsert.assert_not_called()
    metadata.assert_not_called()


def test_bounded_sgs_does_not_start_retry_when_deadline_cannot_cover_backoff(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _, upsert, metadata = _prepare_fetch(
        monkeypatch,
        lambda: FakeResponse([{"data": "01/01/2026", "valor": "1.2"}]),
    )
    bounded = Mock(return_value=InvalidJsonResponse())
    monkeypatch.setattr(bcb_sgs, "get_provider_response_bounded", bounded)
    clock = [0.0]
    monkeypatch.setattr(bcb_sgs, "monotonic", lambda: clock[0])
    monkeypatch.setattr(bcb_sgs, "sleep", lambda delay: clock.__setitem__(0, clock[0] + delay))

    with pytest.raises(ProviderInvalidJSONResponseError):
        bcb_sgs.fetch_sgs_series(
            1635,
            start_date="01/01/2020",
            end_date="31/12/2026",
            use_cache=False,
            mark_refreshed=False,
            fail_fast=True,
            request_timeout=0.5,
        )

    assert bounded.call_count == 2
    upsert.assert_not_called()
    metadata.assert_not_called()


def test_bounded_sgs_rejects_elapsed_deadline_without_request(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    request, upsert, metadata = _prepare_fetch(
        monkeypatch,
        lambda: FakeResponse([{"data": "01/01/2026", "valor": "1.2"}]),
    )
    bounded = Mock()
    monkeypatch.setattr(bcb_sgs, "get_provider_response_bounded", bounded)

    with pytest.raises(TimeoutError, match="deadline elapsed before retry"):
        bcb_sgs.fetch_sgs_series(
            1635,
            start_date="01/01/2020",
            end_date="31/12/2026",
            use_cache=False,
            mark_refreshed=False,
            fail_fast=True,
            request_timeout=0,
        )

    bounded.assert_not_called()
    request.assert_not_called()
    upsert.assert_not_called()
    metadata.assert_not_called()


@pytest.mark.parametrize(
    ("payload", "reason"),
    [
        ({"error": "series unavailable"}, "expected-list"),
        ([{"valor": "1.2"}], "invalid-date"),
        ([{"data": "1/1/2026", "valor": "1.2"}], "invalid-date"),
        ([{"data": "01/01/2026"}], "invalid-value"),
        ([{"data": "2026-01-01", "valor": "0"}], "invalid-date"),
        ([{"data": "01/01/2026", "valor": "not-numeric"}], "invalid-value"),
        ([{"data": "01/01/2026", "valor": "NaN"}], "invalid-value"),
        ([None], "expected-record"),
    ],
)
def test_bounded_sgs_rejects_valid_json_with_invalid_sgs_schema_without_retry_or_writes(
    monkeypatch: pytest.MonkeyPatch,
    payload: object,
    reason: str,
) -> None:
    _, upsert, metadata = _prepare_fetch(monkeypatch, lambda: FakeResponse([]))
    bounded = Mock(return_value=FakeResponse(payload))
    monkeypatch.setattr(bcb_sgs, "get_provider_response_bounded", bounded)
    wait = Mock()
    monkeypatch.setattr(bcb_sgs, "sleep", wait)

    with pytest.raises(bcb_sgs.InvalidSGSPayloadError) as raised:
        bcb_sgs.fetch_sgs_series(
            1635,
            start_date="01/01/2020",
            end_date="31/12/2026",
            use_cache=False,
            fail_fast=True,
            request_timeout=5,
        )

    error = raised.value
    assert error.series_code == 1635
    assert error.start_date == "01/01/2020"
    assert error.end_date == "31/12/2026"
    assert error.reason == reason
    assert "1635" in _error_code(error)
    assert "01/01/2020" in _error_code(error)
    assert bounded.call_count == 1
    wait.assert_not_called()
    upsert.assert_not_called()
    metadata.assert_not_called()


def test_bounded_sgs_keeps_a_valid_empty_window_empty_without_retry(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    request, upsert, metadata = _prepare_fetch(monkeypatch, lambda: FakeResponse([]))
    bounded = Mock(return_value=FakeResponse([]))
    monkeypatch.setattr(bcb_sgs, "get_provider_response_bounded", bounded)

    rows = bcb_sgs.fetch_sgs_series(
        1635,
        start_date="01/01/2020",
        end_date="31/12/2026",
        use_cache=False,
        request_timeout=5,
    )

    assert rows == []
    bounded.assert_called_once()
    request.assert_not_called()
    upsert.assert_not_called()
    metadata.assert_not_called()


def test_bounded_sgs_preserves_a_valid_zero_observation(monkeypatch: pytest.MonkeyPatch) -> None:
    rows = [{"data": "01/01/2026", "valor": "0.00"}]
    request, upsert, metadata = _prepare_fetch(monkeypatch, lambda: FakeResponse(rows))
    bounded = Mock(return_value=FakeResponse(rows))
    monkeypatch.setattr(bcb_sgs, "get_provider_response_bounded", bounded)

    result = bcb_sgs.fetch_sgs_series(
        1635,
        start_date="01/01/2020",
        end_date="31/12/2026",
        use_cache=False,
        request_timeout=5,
    )

    assert result == rows
    request.assert_not_called()
    upsert.assert_called_once()
    assert upsert.call_args.args == (1635, rows)
    assert upsert.call_args.kwargs["deadline_at"] > monotonic()
    assert metadata.call_count == 2


def test_half_open_sgs_actual_producer_preserves_read_timeout_without_retry(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    budget = RetryBudget(RetryProfile("bcb_sgs", 3, 90, 20))
    budget.restrict_to_probe()
    token = install_retry_budget(budget)
    calls = 0

    def timed_out(*_args, **_kwargs):
        nonlocal calls
        calls += 1
        budget.reserve_request()
        raise httpx.ReadTimeout("provider timed out")

    monkeypatch.setattr(bcb_sgs, "get_provider_response_bounded", timed_out)
    monkeypatch.setattr(bcb_sgs, "_window_cache_fresh", lambda *_args, **_kwargs: False)
    try:
        with pytest.raises(httpx.ReadTimeout, match="provider timed out"):
            bcb_sgs.fetch_sgs_window(
                1635, start_date="01/01/2026", end_date="31/12/2026",
                use_cache=False, fail_fast=True, request_timeout=20,
            )
    finally:
        reset_retry_budget(token)

    assert calls == budget.requests == 1
    assert budget.retries == 0


@pytest.mark.parametrize("bounded", [False, True])
def test_sgs_actual_http_trace_binds_nonlist_response_to_iso_window(
    monkeypatch: pytest.MonkeyPatch, bounded: bool,
) -> None:
    body = b'{"a":12345}'
    assert len(body) == 11
    monkeypatch.setattr(bcb_sgs, "get_sgs_range", lambda _code: (None, None))
    monkeypatch.setattr(bcb_sgs, "query_sgs", lambda *_args: [])
    monkeypatch.setattr(bcb_sgs, "upsert_sgs", Mock())
    monkeypatch.setattr(bcb_sgs, "set_meta", Mock())
    request = httpx.Request("GET", "https://api.bcb.gov.br/dados/serie/bcdata.sgs.433/dados")
    response = httpx.Response(200, headers={"content-type": "application/json"}, content=body, request=request)
    if bounded:
        original_client = httpx.AsyncClient

        async def respond(req: httpx.Request) -> httpx.Response:
            return httpx.Response(200, headers={"content-type": "application/json"}, content=body, request=req)

        monkeypatch.setattr(httpx, "AsyncClient", lambda **kwargs: original_client(transport=httpx.MockTransport(respond), **kwargs))
    else:
        monkeypatch.setattr(httpx, "get", lambda *_args, **_kwargs: response)
    budget = RetryBudget(RetryProfile("bcb_sgs", 3, 90, 20))
    token = install_retry_budget(budget)
    try:
        with pytest.raises(InvalidSGSPayloadError):
            bcb_sgs.fetch_sgs_series(
                433, start_date="01/03/2007", end_date="31/03/2007",
                use_cache=False, fail_fast=True, request_timeout=5 if bounded else bcb_sgs.REQUEST_TIMEOUT,
            )
    finally:
        reset_retry_budget(token)
    assert budget.requests == 1
    assert budget.retries == 0
    assert len(budget.http_trace) == 1
    diagnostics = budget.http_trace[0]["diagnostics"]
    assert diagnostics["request_context"] == {
        "provider": "bcb_sgs", "series_code": 433,
        "requested_start": "2007-03-01", "requested_end": "2007-03-31",
        "expected_kind": "list", "local_attempt": 1,
    }
    assert diagnostics["response"]["status_code"] == 200
    assert diagnostics["response"]["bytes_received"] == 11
    assert diagnostics["response"]["body_kind"] == "object"
    assert '"a"' not in json.dumps(diagnostics)
