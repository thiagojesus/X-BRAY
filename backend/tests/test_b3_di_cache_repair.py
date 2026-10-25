import io
import zipfile
from datetime import date, datetime, timedelta, timezone
from unittest.mock import patch

import httpx
import pytest

from datafetchers import b3_di
from datafetchers.focus import FOCUS_12M_KEY
from db import store
from derived.real_rate_inputs import load_di_contracts
from source_ingestion import reset_refresh_context, set_refresh_context
from tests.real_rate_support import _seed_di, _seed_focus, isolated_database

pytestmark = pytest.mark.usefixtures(isolated_database.__name__)


@pytest.mark.parametrize(
    ("cached_dates", "vintage_dates", "expected_dates"),
    [
        (["2026-10-01", "2026-10-02"], [], ["2026-10-02"]),
        (["2026-10-01", "2026-10-02"], ["2026-10-01"], ["2026-10-02"]),
        (["2026-10-01", "2026-10-02"], ["2026-10-02"], []),
        (["2026-10-01", "2026-10-05"], [], ["2026-10-01", "2026-10-02"]),
        (["2026-10-01"], ["2026-10-01"], ["2026-10-02"]),
        (["2026-09-30"], [], ["2026-10-01", "2026-10-02"]),
        ([], [], ["2026-10-01", "2026-10-02"]),
    ],
    ids=[
        "repair-only-newest",
        "older-vintage-does-not-cover-newest",
        "newest-vintage-keeps-cache-network-free",
        "repair-and-normal-missing-date",
        "normal-missing-date-with-covered-cache",
        "outside-window-is-not-replayed",
        "empty-cache-downloads-normal-targets",
    ],
)
def test_download_selection_when_cached_dates_have_vintage_gaps(
    cached_dates: list[str],
    vintage_dates: list[str],
    expected_dates: list[str],
) -> None:
    # Given
    targets = [date(2026, 10, 2), date(2026, 10, 1)]
    stored_rows = [
        {"trade_date": "2026-10-02", "symbol": "DI1V27", "maturity": "2027-10-01", "rate": 11.5}
    ]
    def source_vintages(provider: str, series_key: str) -> list[dict[str, str]]:
        match (provider, series_key):
            case ("b3", "di1"):
                return [{"reference_period": day} for day in vintage_dates]
            case ("focus", key) if key == FOCUS_12M_KEY:
                return []
            case _:
                pytest.fail(f"unexpected vintage source: {provider}/{series_key}")

    with (
        patch.object(b3_di, "list_business_days", return_value=targets),
        patch.object(b3_di, "get_b3_di_dates", return_value=cached_dates),
        patch.object(
            b3_di, "query_observation_vintages",
            side_effect=source_vintages,
        ),
        patch.object(b3_di, "query_b3_di", return_value=stored_rows),
        patch.object(b3_di, "_download_day", return_value=[]) as download,
    ):
        # When
        curves = b3_di.fetch_di_curves(days=2)

    # Then
    assert sorted(call.args[0].isoformat() for call in download.call_args_list) == expected_dates
    assert curves == {
        "2026-10-02": [{"symbol": "DI1V27", "maturity": "2027-10-01", "rate": 11.5}]
    }


def test_repair_is_limited_to_normal_cache_mode() -> None:
    # Given
    with (
        patch.object(b3_di, "list_business_days", return_value=[date(2026, 10, 2)]),
        patch.object(b3_di, "get_b3_di_dates", return_value=["2026-10-02"]),
        patch.object(b3_di, "query_b3_di", return_value=[]),
        patch.object(b3_di, "query_observation_vintages") as vintages,
        patch.object(b3_di, "_download_day") as download,
    ):
        # When
        b3_di.fetch_di_curves(days=1, use_cache=False)

    # Then
    download.assert_not_called()
    vintages.assert_not_called()


def test_cached_refresh_records_one_cache_hit_without_download() -> None:
    run_id = "b3-cache-hit-test"
    token = set_refresh_context(run_id, "b3_di", run_id)
    try:
        with (
            patch.object(b3_di, "list_business_days", return_value=[date(2026, 10, 2)]),
            patch.object(b3_di, "get_b3_di_dates", return_value=["2026-10-02"]),
            patch.object(b3_di, "query_observation_vintages", return_value=[
                {"reference_period": "2026-10-02"}
            ]),
            patch.object(b3_di, "query_b3_di", return_value=[{
                "trade_date": "2026-10-02", "symbol": "DI1V27",
                "maturity": "2027-10-01", "rate": 11.5,
            }]),
            patch.object(b3_di, "_download_day") as download,
        ):
            result = b3_di.fetch_di_curves(days=1)
    finally:
        reset_refresh_context(token)

    rows = [row for row in store.latest_source_extractions(100)
            if row["run_id"] == run_id]
    assert result["2026-10-02"][0]["rate"] == 11.5
    download.assert_not_called()
    assert len(rows) == 1
    assert rows[0]["source"] == "b3"
    assert rows[0]["task_key"] == "b3_di"
    assert rows[0]["status"] == "cache_hit"
    assert rows[0]["network_called"] == 0
    assert rows[0]["extracted_at"] is None
    assert rows[0]["received_count"] == rows[0]["valid_count"] == 0


def test_mixed_cached_and_downloaded_refresh_has_one_network_ledger_row() -> None:
    run_id = "b3-mixed-refresh-test"
    token = set_refresh_context(run_id, "b3_di", run_id)
    downloaded = [{
        "trade_date": "2026-10-02", "symbol": "DI1V27",
        "maturity": "2027-10-01", "rate": 11.5,
    }]
    try:
        with (
            patch.object(b3_di, "list_business_days", return_value=[
                date(2026, 10, 1), date(2026, 10, 2),
            ]),
            patch.object(b3_di, "get_b3_di_dates", return_value=["2026-10-01"]),
            patch.object(b3_di, "query_observation_vintages", return_value=[
                {"reference_period": "2026-10-01"}
            ]),
            patch.object(b3_di, "query_b3_di", return_value=[{
                "trade_date": "2026-10-01", "symbol": "DI1V27",
                "maturity": "2027-10-01", "rate": 11.4,
            }]),
            patch.object(b3_di, "_download_day", return_value=downloaded) as fetch,
        ):
            b3_di.fetch_di_curves(days=2)
    finally:
        reset_refresh_context(token)

    rows = [row for row in store.latest_source_extractions(100)
            if row["run_id"] == run_id]
    fetch.assert_called_once_with(date(2026, 10, 2))
    assert len(rows) == 1
    assert rows[0]["status"] == "success"
    assert rows[0]["network_called"] == 1
    assert rows[0]["received_count"] == rows[0]["valid_count"] == 1
    assert rows[0]["source_reference_min"] == "2026-10-02"
    assert rows[0]["source_reference_max"] == "2026-10-02"
    assert rows[0]["request_count"] == 1


def test_empty_b3_refresh_is_not_mislabeled_as_cache_hit() -> None:
    run_id = "b3-empty-refresh-test"
    token = set_refresh_context(run_id, "b3_di", run_id)
    try:
        with (
            patch.object(b3_di, "list_business_days", return_value=[]),
            patch.object(b3_di, "get_b3_di_dates", return_value=[]),
            patch.object(b3_di, "query_b3_di", return_value=[]),
        ):
            result = b3_di.fetch_di_curves(days=0)
    finally:
        reset_refresh_context(token)

    rows = [row for row in store.latest_source_extractions(100)
            if row["run_id"] == run_id]
    assert result == {}
    assert len(rows) == 1
    assert rows[0]["status"] == "empty"
    assert rows[0]["network_called"] == 0
    assert rows[0]["extracted_at"] is None


def test_derived_inputs_use_retrieval_time_when_legacy_cache_is_repaired() -> None:
    # Given
    targets = [date(2026, 10, 1), date(2026, 10, 2)]
    for target in targets:
        store._execute(
            "INSERT INTO b3_di (trade_date, symbol, maturity, rate, fetched_at) VALUES (?, ?, ?, ?, ?)",
            (target.isoformat(), "DI1V27", "2027-10-01", 10.0, "2026-10-03T14:13:00+00:00"),
        )
    retrieved_at = datetime(2026, 10, 13, 12, tzinfo=timezone.utc)
    xml = b"""<SecurityList xmlns="urn:bvmf.217.01.xsd">
      <PricRpt>
        <TradDt><Dt>2026-10-02</Dt></TradDt>
        <SctyId><TckrSymb>DI1V27</TckrSymb></SctyId>
        <FinInstrmAttrbts><AdjstdQtTax>11.5</AdjstdQtTax></FinInstrmAttrbts>
      </PricRpt>
    </SecurityList>"""
    with io.BytesIO() as archive:
        with zipfile.ZipFile(archive, "w") as zipped:
            zipped.writestr("prices.xml", xml)
        response = httpx.Response(200, content=archive.getvalue())
    with (
        patch.object(b3_di, "list_business_days", return_value=targets),
        patch.object(b3_di, "get_provider_response", return_value=response) as request,
        patch.object(store, "_utc_now", return_value=retrieved_at.isoformat()),
    ):
        # When
        curves = b3_di.fetch_di_curves(days=2)

    # Then
    assert [call.args[0] for call in request.call_args_list] == [
        b3_di.B3_SPR_URL.format(YYMMDD="261002")
    ]
    assert curves["2026-10-01"][0]["rate"] == 10.0
    assert curves["2026-10-02"][0]["rate"] == 11.5
    vintages = store.query_observation_vintages("b3", "di1")
    assert len(vintages) == 1
    assert vintages[0]["reference_period"] == "2026-10-02"
    assert vintages[0]["first_seen_at"] == retrieved_at.isoformat()
    assert vintages[0]["available_at"] == retrieved_at.isoformat()
    assert vintages[0]["source_published_at"] is None
    assert load_di_contracts(as_of=retrieved_at - timedelta(microseconds=1)) == ()
    contracts = load_di_contracts(as_of=retrieved_at)
    assert len(contracts) == 1
    assert contracts[0].trade_date == date(2026, 10, 2)
    assert contracts[0].rate == 11.5
    assert contracts[0].available_at == retrieved_at
    assert contracts[0].availability_basis == "first_seen_at"


@pytest.mark.parametrize(
    ("vintage_dates", "focus_reference", "expected_dates"),
    [
        ([], "2026-10-02", ["2026-10-02", "2026-10-09"]),
        (["2026-10-09"], "2026-10-02", ["2026-10-02"]),
        (["2026-10-09"], "2026-10-03", ["2026-10-02"]),
        (["2026-10-02"], "2026-10-02", ["2026-10-09"]),
        (["2026-10-02", "2026-10-09"], "2026-10-02", []),
        ([], "2026-10-09", ["2026-10-09"]),
        ([], None, ["2026-10-09"]),
        (["2026-10-09"], None, []),
    ],
    ids=[
        "two-distinct-repairs-only",
        "newest-vintage-does-not-cover-focus-aligned-curve",
        "latest-cached-curve-before-focus-reference",
        "focus-aligned-vintage-already-present",
        "both-vintages-present-network-free",
        "same-candidate-deduplicated",
        "future-focus-and-annual-focus-do-not-align",
        "covered-newest-with-future-and-annual-focus-network-free",
    ],
)
def test_repairs_align_with_eligible_focus_when_newest_curve_is_later(
    vintage_dates: list[str],
    focus_reference: str | None,
    expected_dates: list[str],
) -> None:
    # Given
    cutoff = datetime(2026, 10, 13, 22, tzinfo=timezone.utc)
    targets = [date(2026, 10, day) for day in (1, 2, 8, 9)]
    for target in targets:
        store._execute(
            "INSERT INTO b3_di (trade_date, symbol, maturity, rate, fetched_at) VALUES (?, ?, ?, ?, ?)",
            (target.isoformat(), "DI1V27", "2027-10-01", 10.0, "2026-10-03T14:13:00+00:00"),
        )
    for vintage_date in vintage_dates:
        _seed_di(trade_date=vintage_date, available_at=cutoff - timedelta(hours=1))
    if focus_reference is not None:
        _seed_focus(
            reference_date=focus_reference,
            series_key=FOCUS_12M_KEY,
            available_at=cutoff - timedelta(hours=2),
        )
    _seed_focus(
        reference_date="2026-10-08",
        series_key=FOCUS_12M_KEY,
        available_at=cutoff + timedelta(seconds=1),
    )
    _seed_focus(
        reference_date="2026-10-09",
        series_key="IPCA",
        available_at=cutoff - timedelta(hours=2),
    )
    with (
        patch.object(b3_di, "list_business_days", return_value=targets),
        patch.object(b3_di, "datetime", wraps=datetime) as clock,
        patch.object(store, "_utc_now", return_value=cutoff.isoformat()),
        patch.object(b3_di, "_download_day", return_value=[]) as download,
    ):
        clock.now.return_value = cutoff
        # When
        b3_di.fetch_di_curves(days=4)

    # Then
    downloaded = sorted(call.args[0].isoformat() for call in download.call_args_list)
    assert downloaded == expected_dates
    assert len(downloaded) == len(set(downloaded)) <= 2


def test_repair_selection_excludes_focus_vintages_available_after_cutoff() -> None:
    # Given
    cutoff = datetime(2026, 10, 13, 22, tzinfo=timezone.utc)
    _seed_focus(
        reference_date="2026-10-02", series_key=FOCUS_12M_KEY,
        available_at=cutoff - timedelta(hours=1),
    )
    _seed_focus(
        reference_date="2026-10-08", series_key=FOCUS_12M_KEY,
        available_at=cutoff + timedelta(seconds=1),
    )
    _seed_di(trade_date="2026-10-09", available_at=cutoff - timedelta(hours=1))
    for reference in ("2026-10-02", "2026-10-08"):
        store._execute(
            "INSERT INTO b3_di (trade_date, symbol, maturity, rate, fetched_at) VALUES (?, ?, ?, ?, ?)",
            (reference, "DI1V27", "2027-10-01", 99.0, "2026-10-03T14:13:00+00:00"),
        )
    with (
        patch.object(
            b3_di, "list_business_days",
            return_value=[date(2026, 10, day) for day in (2, 8, 9)],
        ),
        patch.object(b3_di, "datetime", wraps=datetime) as clock,
        patch.object(store, "_utc_now", return_value=cutoff.isoformat()),
        patch.object(b3_di, "_download_day", return_value=[]) as download,
    ):
        clock.now.return_value = cutoff
        # When
        b3_di.fetch_di_curves(days=3)

    # Then
    assert [call.args[0].isoformat() for call in download.call_args_list] == ["2026-10-02"]
