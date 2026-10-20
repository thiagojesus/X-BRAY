from datetime import date, datetime, timezone
from zoneinfo import ZoneInfo

from indicator_catalog import INDICATOR_CATALOG, Frequency, IndicatorKey
from macro_score.inputs import (
    AvailableInput,
    CurrentInputSelection,
    InputSelectionReason,
    ScoreSourcePolicy,
    ScoreVintage,
    UnavailableInput,
    historical_month_window,
    score_cutoff,
    select_current_source,
    select_current_vintage,
    select_historical_vintages,
)


def _vintage(reference_period: date, available_at: datetime) -> ScoreVintage:
    return ScoreVintage(
        provider="bcb_sgs",
        series_key="desemprego",
        reference_period=reference_period,
        value=float(reference_period.day),
        unit="pct",
        source="fixture SGS series",
        available_at=available_at,
        first_seen_at=available_at,
        payload_hash=available_at.isoformat(),
    )


def _expect_available(
    selection: CurrentInputSelection,
    expected: ScoreVintage,
) -> None:
    assert isinstance(selection, AvailableInput)
    assert selection.observation == expected


def _expect_unavailable(
    selection: CurrentInputSelection,
    expected: InputSelectionReason,
) -> None:
    assert isinstance(selection, UnavailableInput)
    assert selection.reason is expected


def test_red_xbr_601_score_cutoff_uses_sao_paulo_month_and_120_month_window() -> None:
    cutoff = score_cutoff(datetime(2026, 10, 1, 1, tzinfo=timezone.utc))
    window = historical_month_window(cutoff.score_month)

    assert cutoff.as_of == datetime(2026, 10, 1, 1, tzinfo=timezone.utc)
    assert cutoff.score_month == date(2026, 9, 1)
    assert len(window) == 120
    assert window[0] == date(2016, 10, 1)
    assert window[-1] == date(2026, 9, 1)


def test_current_vintage_selects_only_revisions_known_by_cutoff() -> None:
    spec = INDICATOR_CATALOG[IndicatorKey("desemprego")]
    first_seen = datetime(2026, 9, 10, tzinfo=timezone.utc)
    revised_at = datetime(2026, 9, 20, tzinfo=timezone.utc)
    original = _vintage(date(2026, 8, 1), first_seen)
    revised = original.model_copy(update={
        "value": 9.5,
        "available_at": revised_at,
        "first_seen_at": revised_at,
        "payload_hash": "revision-2",
    })

    before_revision = select_current_vintage(
        (original, revised),
        score_cutoff(datetime(2026, 9, 15, tzinfo=timezone.utc)),
        spec,
    )
    after_revision = select_current_vintage(
        (original, revised),
        score_cutoff(datetime(2026, 9, 21, tzinfo=timezone.utc)),
        spec,
    )

    _expect_available(before_revision, original)
    _expect_available(after_revision, revised)


def test_current_monthly_deadline_is_inclusive_in_sao_paulo() -> None:
    spec = INDICATOR_CATALOG[IndicatorKey("ipca_12m")]
    vintage = _vintage(
        date(2026, 6, 1), datetime(2026, 7, 10, tzinfo=timezone.utc)
    ).model_copy(update={"series_key": "ipca_12m"})
    before_local_deadline_end = score_cutoff(datetime(2026, 9, 14, 2, 59, tzinfo=timezone.utc))
    after_local_deadline_end = score_cutoff(datetime(2026, 9, 14, 3, 0, tzinfo=timezone.utc))

    eligible = select_current_vintage((vintage,), before_local_deadline_end, spec)
    stale = select_current_vintage((vintage,), after_local_deadline_end, spec)

    assert before_local_deadline_end.as_of.astimezone(ZoneInfo("America/Sao_Paulo")).date() == date(2026, 9, 13)
    _expect_available(eligible, vintage)
    _expect_unavailable(stale, InputSelectionReason.STALE)


def test_bcb_credit_monthly_lag_uses_two_calendar_month_publication_window() -> None:
    cutoff = score_cutoff(datetime(2026, 10, 19, 16, tzinfo=timezone.utc))
    spec = INDICATOR_CATALOG[IndicatorKey("inadimplencia_pf")]
    august = _vintage(date(2026, 8, 1), datetime(2026, 10, 13, tzinfo=timezone.utc))
    july = _vintage(date(2026, 7, 1), datetime(2026, 9, 13, tzinfo=timezone.utc))
    august = august.model_copy(update={"series_key": "inadimplencia_pf"})
    july = july.model_copy(update={"series_key": "inadimplencia_pf"})

    eligible = select_current_vintage((august,), cutoff, spec)
    stale = select_current_vintage((july,), cutoff, spec)

    _expect_available(eligible, august)
    _expect_unavailable(stale, InputSelectionReason.STALE)


def test_bcb_credit_monthly_reference_cannot_be_after_score_month() -> None:
    cutoff = score_cutoff(datetime(2026, 10, 19, 16, tzinfo=timezone.utc))
    spec = INDICATOR_CATALOG[IndicatorKey("inadimplencia_pf")]
    future = _vintage(date(2026, 11, 1), datetime(2026, 10, 19, tzinfo=timezone.utc))
    future = future.model_copy(update={"series_key": "inadimplencia_pf"})

    selected = select_current_vintage((future,), cutoff, spec)

    _expect_unavailable(selected, InputSelectionReason.REQUIRED_INPUT_MISSING)


def test_historical_samples_deduplicate_months_without_applying_current_freshness() -> None:
    spec = INDICATOR_CATALOG[IndicatorKey("desemprego")]
    cutoff = score_cutoff(datetime(2026, 10, 10, 3, tzinfo=timezone.utc))
    stale_month = _vintage(date(2026, 5, 1), datetime(2026, 6, 1, tzinfo=timezone.utc))
    revision = stale_month.model_copy(update={
        "value": 7.2,
        "available_at": datetime(2026, 7, 1, tzinfo=timezone.utc),
        "first_seen_at": datetime(2026, 7, 1, tzinfo=timezone.utc),
        "payload_hash": "late-revision",
    })
    oldest_in_window = _vintage(date(2016, 11, 1), datetime(2016, 12, 1, tzinfo=timezone.utc))
    outside_window = _vintage(date(2016, 10, 1), datetime(2016, 11, 1, tzinfo=timezone.utc))
    future_publication = _vintage(date(2026, 8, 1), datetime(2026, 10, 11, tzinfo=timezone.utc))

    history = select_historical_vintages(
        (stale_month, revision, oldest_in_window, outside_window, future_publication),
        cutoff,
        spec,
    )

    window = historical_month_window(cutoff.score_month)
    assert len(window) == 120
    assert window[0] == date(2016, 11, 1)
    assert window[-1] == date(2026, 10, 1)
    assert history == (oldest_in_window, revision)


def test_daily_current_selection_does_not_use_a_reference_after_cutoff_date() -> None:
    spec = INDICATOR_CATALOG[IndicatorKey("selic_meta")]
    cutoff = score_cutoff(datetime(2026, 9, 1, 10, tzinfo=timezone.utc))
    previous = _vintage(
        date(2026, 8, 31), datetime(2026, 9, 1, 8, tzinfo=timezone.utc)
    ).model_copy(update={"series_key": "selic_meta", "unit": "pct_annual"})
    same_date = previous.model_copy(update={
        "reference_period": date(2026, 9, 1),
        "value": 15.0,
        "available_at": datetime(2026, 9, 1, 9, tzinfo=timezone.utc),
        "first_seen_at": datetime(2026, 9, 1, 9, tzinfo=timezone.utc),
        "payload_hash": "same-date",
    })
    future_date = same_date.model_copy(update={
        "reference_period": date(2026, 9, 2),
        "available_at": datetime(2026, 9, 1, 9, 30, tzinfo=timezone.utc),
        "payload_hash": "future-reference",
    })

    selected = select_current_vintage((previous, same_date, future_date), cutoff, spec)

    _expect_available(selected, same_date)


def test_focus_policy_selects_only_the_exact_twelve_month_series() -> None:
    cutoff = score_cutoff(datetime(2026, 9, 21, tzinfo=timezone.utc))
    focus_policy = ScoreSourcePolicy(
        provider="focus",
        series_key="IPCA_12M_S_BASE0",
        frequency=Frequency.DAILY,
        freshness_days=1,
    )
    exact = _vintage(
        date(2026, 9, 18), datetime(2026, 9, 20, tzinfo=timezone.utc)
    ).model_copy(update={
        "provider": "focus",
        "series_key": "IPCA_12M_S_BASE0",
        "unit": "pct_annual",
        "source": "Focus IPCA 12m",
    })
    annual_fallback = exact.model_copy(update={"series_key": "IPCA"})

    selected = select_current_source((annual_fallback, exact), cutoff, focus_policy)

    _expect_available(selected, exact)
