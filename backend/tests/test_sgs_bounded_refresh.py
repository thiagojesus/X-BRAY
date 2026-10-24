from collections.abc import Callable
from unittest.mock import Mock

import pytest

import datafetchers.bcb_sgs as bcb_sgs


class FakeResponse:
    def __init__(self, rows: list[dict[str, str]]) -> None:
        self.rows = rows

    def json(self) -> list[dict[str, str]]:
        return self.rows


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
    assert bounded.call_args.kwargs["deadline_seconds"] == 5
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
