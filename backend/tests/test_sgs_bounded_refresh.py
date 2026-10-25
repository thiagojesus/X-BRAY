import json
from collections.abc import Callable
from typing import ClassVar
from unittest.mock import Mock

import pytest

from datafetchers import bcb_sgs
from datafetchers.bcb_sgs import InvalidSGSPayloadError
from provider_http import ProviderInvalidJSONResponseError
from refresh import _error_code


class FakeResponse:
    def __init__(self, payload: object) -> None:
        self.payload = payload

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


def test_sgs_refresh_plan_is_four_independent_ten_year_windows() -> None:
    assert bcb_sgs.sgs_refresh_windows() == (
        ("01/01/1990", "01/01/2000"),
        ("01/01/2000", "01/01/2010"),
        ("01/01/2010", "01/01/2020"),
        ("01/01/2020", bcb_sgs._format_date(bcb_sgs.date.today())),
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
        {"data": "01/01/2015", "valor": "1.0"},
        {"data": "01/01/2015", "valor": "1.00"},
        {"data": "02/01/2015", "valor": "0"},
    ]
    monkeypatch.setattr(bcb_sgs, "query_sgs", lambda *_args: [])
    monkeypatch.setattr(bcb_sgs, "_window_cache_fresh", lambda *_args: False)
    monkeypatch.setattr(bcb_sgs, "get_sgs_range", lambda _code: (None, None))
    monkeypatch.setattr(bcb_sgs, "upsert_sgs", Mock())
    monkeypatch.setattr(bcb_sgs, "set_meta", Mock())
    bounded = Mock(return_value=FakeResponse(payload))
    monkeypatch.setattr(bcb_sgs, "get_provider_response_bounded", bounded)

    result = bcb_sgs.fetch_sgs_window(
        226, start_date="01/01/2015", end_date="31/12/2015",
        use_cache=False, fail_fast=True, request_timeout=20,
    )

    assert result == [payload[0], payload[2]]
    assert bounded.call_args.kwargs["deadline_seconds"] <= 20
    bcb_sgs.upsert_sgs.assert_called_once_with(226, [payload[0], payload[2]])


def test_annual_window_source_rejects_conflicting_duplicates_without_store(monkeypatch) -> None:
    payload = [
        {"data": "01/01/2015", "valor": "1.0"},
        {"data": "01/01/2015", "valor": "2.0"},
    ]
    monkeypatch.setattr(bcb_sgs, "query_sgs", lambda *_args: [])
    monkeypatch.setattr(bcb_sgs, "_window_cache_fresh", lambda *_args: False)
    monkeypatch.setattr(bcb_sgs, "get_sgs_range", lambda _code: (None, None))
    upsert = Mock()
    monkeypatch.setattr(bcb_sgs, "upsert_sgs", upsert)
    monkeypatch.setattr(bcb_sgs, "set_meta", Mock())
    monkeypatch.setattr(bcb_sgs, "get_provider_response_bounded", Mock(return_value=FakeResponse(payload)))

    with pytest.raises(InvalidSGSPayloadError, match="conflicting-duplicate-date"):
        bcb_sgs.fetch_sgs_window(
            226, start_date="01/01/2015", end_date="31/12/2015",
            use_cache=False, fail_fast=True, request_timeout=20,
        )
    upsert.assert_not_called()


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
    upsert.assert_called_once_with(1635, rows)
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
    upsert.assert_called_once_with(1635, rows)
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
    upsert.assert_called_once_with(1635, rows)
    assert metadata.call_count == 2
    request.assert_not_called()
