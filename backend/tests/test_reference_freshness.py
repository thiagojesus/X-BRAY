from datetime import date

import pytest

from reference_freshness import resolve_reference_freshness


@pytest.mark.parametrize(("reference", "expected"), [
    (date(2024, 2, 1), date(2024, 2, 29)),
    (date(2025, 2, 1), date(2025, 2, 28)),
    (date(2026, 3, 1), date(2026, 3, 31)),
])
def test_monthly_end(reference: date, expected: date) -> None:
    result = resolve_reference_freshness(reference, expected, "monthly", "date_range", 75, series_code=1635, identity_kind="point")
    assert result is not None and result.basis_date == expected


@pytest.mark.parametrize(("reference", "expected"), [
    (date(2026, 1, 4), date(2026, 3, 31)),
    (date(2026, 4, 1), date(2026, 6, 30)),
    (date(2026, 7, 1), date(2026, 9, 30)),
    (date(2026, 10, 1), date(2026, 12, 31)),
])
def test_quarter_end(reference: date, expected: date) -> None:
    result = resolve_reference_freshness(reference, expected, "quarterly", "date_range", 45, series_code=1207, identity_kind="point")
    assert result is not None and result.basis_date == expected


def test_moving_quarter_uses_reference_month_and_full_period_uses_start() -> None:
    moving = resolve_reference_freshness(date(2026, 8, 1), date(2026, 10, 28), "moving_quarter", "date_range", 75, series_code=1635, identity_kind="point")
    assert moving is not None and moving.basis_date == date(2026, 8, 31)
    full = resolve_reference_freshness(date(2026, 8, 1), date(2026, 10, 28), "monthly", "full_period", 75, series_code=226, identity_kind="full_period")
    assert full is not None and full.basis_date == date(2026, 8, 1) and full.stale


def test_annual_daily_and_market_and_inclusive_boundary() -> None:
    annual = resolve_reference_freshness(date(2025, 1, 1), date(2025, 12, 31), "annual", "date_range", 1, series_code=1207, identity_kind="point")
    assert annual is not None and annual.basis_date == date(2025, 12, 31)
    for frequency in ("daily", "market"):
        result = resolve_reference_freshness(date(2026, 10, 28), date(2026, 10, 29), frequency, "date_range", 1, series_code=1635, identity_kind="point")
        assert result is not None and not result.stale
    assert resolve_reference_freshness(date(2026, 8, 1), date(2026, 11, 14), "monthly", "date_range", 75, series_code=1635, identity_kind="point").stale is False
    assert resolve_reference_freshness(date(2026, 8, 1), date(2026, 11, 15), "monthly", "date_range", 75, series_code=1635, identity_kind="point").stale


@pytest.mark.parametrize("frequency,days", [("other", 75), ("monthly", True), ("monthly", 0)])
def test_invalid_policy_is_unverified(frequency: str, days: int) -> None:
    assert resolve_reference_freshness(date(2026, 1, 1), date(2026, 2, 1), frequency, "date_range", days, series_code=1635, identity_kind="point") is None


def test_unknown_method_and_future_observation_are_unverified() -> None:
    assert resolve_reference_freshness(date(2026, 1, 1), date(2026, 2, 1), "monthly", "unknown", 75, series_code=1635, identity_kind="point") is None
    assert resolve_reference_freshness(date(2026, 3, 1), date(2026, 2, 1), "monthly", "date_range", 75, series_code=1635, identity_kind="point") is None
    assert resolve_reference_freshness(date(2026, 1, 1), date(2026, 2, 1), "unknown", "full_period", 75, series_code=226, identity_kind="full_period") is None


def test_full_period_preserves_eligible_start_and_overflow_is_unverified() -> None:
    full_period = resolve_reference_freshness(
        date(2026, 8, 1), date(2026, 10, 28), "monthly", "full_period", 75,
        series_code=226, identity_kind="full_period",
    )
    assert full_period is not None and full_period.basis_date == date(2026, 8, 1)
    assert resolve_reference_freshness(
        date.max, date.max, "daily", "date_range", 1,
        series_code=1635, identity_kind="point",
    ) is None


@pytest.mark.parametrize(("code", "identity", "frequency", "method"), [
    (1635, "point", "monthly", "full_period"),
    (226, "point", "monthly", "full_period"),
    (226, "full_period", "monthly", "date_range"),
    (226, "full_period", "annual", "full_period"),
    (226, "full_period", "daily", "full_period"),
    (226, "full_period", "unknown", "full_period"),
])
def test_full_period_requires_trusted_tr_contract_context(
    code: int, identity: str, frequency: str, method: str,
) -> None:
    assert resolve_reference_freshness(
        date(2026, 8, 1), date(2026, 10, 28), frequency, method, 75,
        series_code=code, identity_kind=identity,
    ) is None


@pytest.mark.parametrize(("code", "identity"), [(None, "point"), (True, "point"), (0, "point"), (-1, "point"), (1635, None)])
def test_missing_or_invalid_unit_context_is_unverified(code: object, identity: object) -> None:
    assert resolve_reference_freshness(
        date(2026, 8, 1), date(2026, 10, 28), "monthly", "date_range", 75,
        series_code=code, identity_kind=identity,
    ) is None


@pytest.mark.parametrize(("frequency", "method", "identity"), [
    ([], "date_range", "point"),
    ({}, "date_range", "point"),
    (None, "date_range", "point"),
    ("monthly", [], "point"),
    ("monthly", {}, "point"),
    ("monthly", None, "point"),
    ("monthly", "date_range", []),
    ("monthly", "date_range", {}),
    ("monthly", "date_range", None),
])
def test_non_string_policy_context_is_unverified(
    frequency: object, method: object, identity: object,
) -> None:
    assert resolve_reference_freshness(
        date(2026, 8, 1), date(2026, 10, 28), frequency, method, 75,
        series_code=226, identity_kind=identity,
    ) is None
