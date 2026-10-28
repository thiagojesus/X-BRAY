"""Calendar interpretation for reporting reference-period freshness.

The as-of date is the UTC calendar date supplied by the reporting projection.
This is distinct from capture-time freshness and publisher publication cadence.
"""

from __future__ import annotations

import calendar
from dataclasses import dataclass
from datetime import date, timedelta

POLICY_VERSION = "reference-freshness-v2"


@dataclass(frozen=True, slots=True)
class ReferenceFreshness:
    basis_date: date
    basis_kind: str
    deadline: date
    age_days: int
    stale: bool


def _month_end(day: date) -> date:
    return date(day.year, day.month, calendar.monthrange(day.year, day.month)[1])


def resolve_reference_freshness(
    reference: date,
    as_of: date,
    frequency: object,
    method: object,
    freshness_days: object,
    *,
    series_code: object,
    identity_kind: object,
) -> ReferenceFreshness | None:
    """Resolve a trusted frozen-unit cadence; reject ambiguous unit context."""
    if (
        not isinstance(frequency, str)
        or not isinstance(method, str)
        or not isinstance(identity_kind, str)
        or type(series_code) is not int
        or series_code < 1
        or identity_kind not in {"point", "full_period"}
        or type(freshness_days) is not int
        or freshness_days < 1
        or frequency not in {"daily", "market", "monthly", "moving_quarter", "quarterly", "annual"}
        or method not in {"date_range", "full_period"}
        or reference > as_of
    ):
        return None
    days = freshness_days
    basis_kind: str
    if method == "full_period":
        if not (
            series_code == 226
            and identity_kind == "full_period"
            and frequency == "monthly"
        ):
            return None
        basis_date, basis_kind = reference, "eligible_period_start"
    elif identity_kind != "point":
        return None
    elif frequency == "monthly":
        basis_date, basis_kind = _month_end(reference), "observation_month_end"
    elif frequency == "moving_quarter":
        basis_date, basis_kind = _month_end(reference), "reference_month_end"
    elif frequency == "quarterly":
        quarter_end_month = ((reference.month - 1) // 3 + 1) * 3
        basis_date = date(reference.year, quarter_end_month,
                          calendar.monthrange(reference.year, quarter_end_month)[1])
        basis_kind = "calendar_quarter_end"
    elif frequency == "annual":
        basis_date, basis_kind = date(reference.year, 12, 31), "calendar_year_end"
    elif frequency in {"daily", "market"}:
        basis_date, basis_kind = reference, "observation_date"
    else:
        return None
    age_days = (as_of - basis_date).days
    try:
        deadline = basis_date + timedelta(days=days)
    except OverflowError:
        return None
    return ReferenceFreshness(
        basis_date=basis_date,
        basis_kind=basis_kind,
        deadline=deadline,
        age_days=age_days,
        stale=as_of > deadline,
    )
