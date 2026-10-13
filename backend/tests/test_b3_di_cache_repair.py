import io
import zipfile
from datetime import date, datetime, timedelta, timezone
from unittest.mock import patch

import httpx
import pytest

from datafetchers import b3_di
from db import store
from derived.real_rate_inputs import load_di_contracts
from tests.real_rate_support import isolated_database

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
    with (
        patch.object(b3_di, "list_business_days", return_value=targets),
        patch.object(b3_di, "get_b3_di_dates", return_value=cached_dates),
        patch.object(
            b3_di, "query_observation_vintages",
            return_value=[{"reference_period": day} for day in vintage_dates],
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
