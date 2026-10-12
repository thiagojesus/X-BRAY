from calendar import monthrange
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from enum import StrEnum, unique
from typing import Final, TypeAlias, assert_never
from zoneinfo import ZoneInfo

from pydantic import BaseModel, ConfigDict, Field, field_validator

from derived.models import utc_datetime
from indicator_catalog import Frequency, IndicatorSpec

_SCORE_ZONE: Final = ZoneInfo("America/Sao_Paulo")
_WINDOW_MONTHS: Final = 120


class ScoreVintage(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    provider: str = Field(min_length=1)
    series_key: str = Field(min_length=1)
    reference_period: date
    value: float = Field(allow_inf_nan=False)
    unit: str = Field(min_length=1)
    source: str = Field(min_length=1)
    available_at: datetime
    first_seen_at: datetime
    payload_hash: str = Field(min_length=1)
    source_published_at: datetime | None = None

    @field_validator("available_at", "first_seen_at", "source_published_at")
    @classmethod
    def _utc_timestamp(cls, value: datetime | None) -> datetime | None:
        return utc_datetime(value) if value is not None else None

    @property
    def availability_basis(self) -> str:
        return "source_published_at" if self.source_published_at is not None else "first_seen_at"


@dataclass(frozen=True, slots=True)
class MacroScoreCutoff:
    as_of: datetime
    score_month: date


@dataclass(frozen=True, slots=True)
class ScoreSourcePolicy:
    provider: str
    series_key: str
    frequency: Frequency
    freshness_days: int


@unique
class InputSelectionReason(StrEnum):
    REQUIRED_INPUT_MISSING = "required_input_missing"
    STALE = "stale_input"


@dataclass(frozen=True, slots=True)
class AvailableInput:
    observation: ScoreVintage


@dataclass(frozen=True, slots=True)
class UnavailableInput:
    reason: InputSelectionReason


CurrentInputSelection: TypeAlias = AvailableInput | UnavailableInput


def score_cutoff(as_of: datetime) -> MacroScoreCutoff:
    cutoff = utc_datetime(as_of)
    local_date = cutoff.astimezone(_SCORE_ZONE).date()
    return MacroScoreCutoff(cutoff, date(local_date.year, local_date.month, 1))


def _shift_month(month: date, amount: int) -> date:
    index = month.year * 12 + month.month - 1 + amount
    return date(index // 12, index % 12 + 1, 1)


def historical_month_window(score_month: date) -> tuple[date, ...]:
    anchor = date(score_month.year, score_month.month, 1)
    return tuple(
        _shift_month(anchor, offset)
        for offset in range(-(_WINDOW_MONTHS - 1), 1)
    )


def _period_end(reference_period: date, frequency: Frequency) -> date | None:
    match frequency:
        case Frequency.MONTHLY | Frequency.MOVING_QUARTER:
            return date(
                reference_period.year,
                reference_period.month,
                monthrange(reference_period.year, reference_period.month)[1],
            )
        case Frequency.QUARTERLY:
            end_month = ((reference_period.month - 1) // 3 + 1) * 3
            return date(reference_period.year, end_month, monthrange(reference_period.year, end_month)[1])
        case Frequency.ANNUAL:
            return date(reference_period.year, 12, 31)
        case Frequency.DAILY | Frequency.MARKET:
            return None
    return assert_never(frequency)


def current_reference_is_fresh(
    reference_period: date,
    cutoff: MacroScoreCutoff,
    policy: ScoreSourcePolicy,
) -> bool:
    period_end = _period_end(reference_period, policy.frequency)
    if period_end is None:
        return True
    deadline = period_end + timedelta(days=policy.freshness_days)
    return cutoff.as_of.astimezone(_SCORE_ZONE).date() <= deadline


def _known_vintages(
    vintages: Sequence[ScoreVintage],
    *,
    cutoff: MacroScoreCutoff,
    policy: ScoreSourcePolicy,
) -> tuple[ScoreVintage, ...]:
    return tuple(
        vintage
        for vintage in vintages
        if vintage.provider == policy.provider
        and vintage.series_key == policy.series_key
        and vintage.available_at <= cutoff.as_of
    )


def _latest_by_reference_period(
    vintages: Sequence[ScoreVintage],
) -> tuple[ScoreVintage, ...]:
    latest: dict[date, ScoreVintage] = {}
    for vintage in vintages:
        previous = latest.get(vintage.reference_period)
        if previous is None or (
            vintage.available_at,
            vintage.first_seen_at,
            vintage.payload_hash,
        ) > (
            previous.available_at,
            previous.first_seen_at,
            previous.payload_hash,
        ):
            latest[vintage.reference_period] = vintage
    return tuple(latest[key] for key in sorted(latest))


def _latest_by_month(vintages: Sequence[ScoreVintage]) -> tuple[ScoreVintage, ...]:
    latest: dict[date, ScoreVintage] = {}
    for vintage in vintages:
        month = date(vintage.reference_period.year, vintage.reference_period.month, 1)
        previous = latest.get(month)
        if previous is None or (
            vintage.reference_period,
            vintage.available_at,
            vintage.first_seen_at,
            vintage.payload_hash,
        ) > (
            previous.reference_period,
            previous.available_at,
            previous.first_seen_at,
            previous.payload_hash,
        ):
            latest[month] = vintage
    return tuple(latest[key] for key in sorted(latest))


def select_known_source_vintages(
    vintages: Sequence[ScoreVintage],
    cutoff: MacroScoreCutoff,
    policy: ScoreSourcePolicy,
) -> tuple[ScoreVintage, ...]:
    return _latest_by_reference_period(
        _known_vintages(vintages, cutoff=cutoff, policy=policy)
    )


def select_latest_source_by_month(
    vintages: Sequence[ScoreVintage],
) -> tuple[ScoreVintage, ...]:
    return _latest_by_month(vintages)


def select_current_vintage(
    vintages: Sequence[ScoreVintage],
    cutoff: MacroScoreCutoff,
    spec: IndicatorSpec,
) -> CurrentInputSelection:
    return select_current_source(
        vintages,
        cutoff,
        ScoreSourcePolicy(
            spec.provider.value,
            str(spec.key),
            spec.frequency,
            spec.freshness_days,
        ),
    )


def select_current_source(
    vintages: Sequence[ScoreVintage],
    cutoff: MacroScoreCutoff,
    policy: ScoreSourcePolicy,
) -> CurrentInputSelection:
    local_date = cutoff.as_of.astimezone(_SCORE_ZONE).date()
    candidates = tuple(
        vintage
        for vintage in select_known_source_vintages(vintages, cutoff, policy)
        if date(vintage.reference_period.year, vintage.reference_period.month, 1) <= cutoff.score_month
        and (
            policy.frequency not in (Frequency.DAILY, Frequency.MARKET)
            or vintage.reference_period <= local_date
        )
    )
    if not candidates:
        return UnavailableInput(InputSelectionReason.REQUIRED_INPUT_MISSING)

    latest = max(candidates, key=lambda item: item.reference_period)
    if not current_reference_is_fresh(latest.reference_period, cutoff, policy):
        return UnavailableInput(InputSelectionReason.STALE)
    return AvailableInput(latest)


def select_historical_vintages(
    vintages: Sequence[ScoreVintage],
    cutoff: MacroScoreCutoff,
    spec: IndicatorSpec,
) -> tuple[ScoreVintage, ...]:
    return select_historical_source(
        vintages,
        cutoff,
        ScoreSourcePolicy(
            spec.provider.value,
            str(spec.key),
            spec.frequency,
            spec.freshness_days,
        ),
    )


def select_historical_source(
    vintages: Sequence[ScoreVintage],
    cutoff: MacroScoreCutoff,
    policy: ScoreSourcePolicy,
) -> tuple[ScoreVintage, ...]:
    window = frozenset(historical_month_window(cutoff.score_month))
    period_eligible = tuple(
        vintage
        for vintage in _known_vintages(vintages, cutoff=cutoff, policy=policy)
        if date(vintage.reference_period.year, vintage.reference_period.month, 1) in window
    )
    return _latest_by_month(period_eligible)
