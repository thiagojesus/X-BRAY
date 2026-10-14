import io
import zipfile
from datetime import date, datetime, timedelta, timezone
from unittest.mock import patch

import httpx
import pytest

from datafetchers import b3_di
from datafetchers.focus import FOCUS_12M_KEY
from db import store
from derived.models import UnavailableReason
from derived.real_rate import build_real_rate_result
from tests.real_rate_support import _seed_di, _seed_focus, isolated_database

pytestmark = pytest.mark.usefixtures(isolated_database.__name__)


@pytest.fixture
def focus_aligned_retrieval() -> tuple[datetime, list[str]]:
    retrieved_at = datetime(2026, 10, 13, 22, tzinfo=timezone.utc)
    _seed_focus(
        reference_date="2026-10-02",
        median=4.66,
        series_key=FOCUS_12M_KEY,
        available_at=datetime(2026, 10, 3, 14, tzinfo=timezone.utc),
    )
    _seed_focus(
        reference_date="2026-10-08",
        median=8.0,
        series_key=FOCUS_12M_KEY,
        available_at=retrieved_at + timedelta(seconds=1),
    )
    _seed_di(
        trade_date="2026-10-09",
        maturity="2027-10-01",
        rate=12.0,
        available_at=retrieved_at - timedelta(minutes=14),
    )
    store._execute(
        "INSERT INTO b3_di (trade_date, symbol, maturity, rate, fetched_at) VALUES (?, ?, ?, ?, ?)",
        ("2026-10-02", "DI1V27", "2027-10-01", 99.0, "2026-10-03T14:13:00+00:00"),
    )
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
        patch.object(
            b3_di, "list_business_days",
            return_value=[date(2026, 10, 2), date(2026, 10, 9)],
        ),
        patch.object(b3_di, "datetime", wraps=datetime) as clock,
        patch.object(store, "_utc_now", return_value=retrieved_at.isoformat()),
        patch.object(b3_di, "get_provider_response", return_value=response) as request,
    ):
        clock.now.return_value = retrieved_at
        b3_di.fetch_di_curves(days=2)
    return retrieved_at, [call.args[0] for call in request.call_args_list]


def test_cached_refresh_requests_focus_aligned_date_when_latest_di_has_a_vintage(
    focus_aligned_retrieval: tuple[datetime, list[str]],
) -> None:
    # Given
    expected_url = b3_di.B3_SPR_URL.format(YYMMDD="261002")

    # When
    _, requested_urls = focus_aligned_retrieval

    # Then
    assert requested_urls == [expected_url]


@pytest.mark.parametrize("elapsed_microseconds", [0, 1], ids=["at-retrieval", "after-retrieval"])
def test_real_rate_uses_focus_aligned_curve_when_freshly_retrieved(
    focus_aligned_retrieval: tuple[datetime, list[str]],
    elapsed_microseconds: int,
) -> None:
    # Given
    retrieved_at, _ = focus_aligned_retrieval
    cutoff = retrieved_at + timedelta(microseconds=elapsed_microseconds)

    # When
    result = build_real_rate_result(as_of=cutoff, history=())

    # Then
    expected = ((1 + 11.5 / 100) / (1 + 4.66 / 100) - 1) * 100
    assert result.value == expected, f"actual unavailable reason: {result.reason}"
    assert result.reason is None
    assert result.reference_date == "2026-10-02"
    nominal = next(item for item in result.inputs if item.key == "nominal_rate")
    assert nominal.reference_period == "2026-10-02"
    assert nominal.value == 11.5
    assert nominal.available_at == retrieved_at
    assert dict(nominal.metadata) == {
        "availability_basis": "first_seen_at",
        "maturity": "2027-10-01",
        "symbol": "DI1V27",
    }
    assert result.historical_percentile is None
    assert result.classification_reason == "insufficient_history"


def test_real_rate_excludes_legacy_curve_when_cutoff_precedes_fresh_retrieval(
    focus_aligned_retrieval: tuple[datetime, list[str]],
) -> None:
    # Given
    retrieved_at, _ = focus_aligned_retrieval
    cutoff = retrieved_at - timedelta(microseconds=1)

    # When
    result = build_real_rate_result(as_of=cutoff, history=())

    # Then
    assert result.value is None
    assert result.reason is UnavailableReason.DI_CURVE_MISSING
    assert result.reference_date == "2026-10-02"
    assert all(item.key != "nominal_rate" for item in result.inputs)
