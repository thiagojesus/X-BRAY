from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone
from math import isfinite
from threading import RLock
from typing import Final, Literal, Protocol, TypeAlias, TypedDict

from indicator_catalog import Frequency

Scalar: TypeAlias = str | int | float | bool | None
Record: TypeAlias = dict[str, Scalar]
SeriesMap: TypeAlias = dict[str, list[Record]]
DatasetStatus: TypeAlias = Literal["ok", "partial", "unavailable"]


class FreshnessSpec(Protocol):
    frequency: Frequency
    freshness_days: int


@dataclass(frozen=True, slots=True)
class StaticFreshnessSpec:
    frequency: Frequency
    freshness_days: int


class SeriesMetadata(TypedDict):
    latest_reference: str
    deadline: str
    freshness: Literal["ok", "stale"]
    observations: int


class UnavailableSeries(TypedDict):
    series: str
    required: bool
    reason: str


class QualityPayload(TypedDict):
    dataset: str
    status: DatasetStatus
    last_updated: str | None
    metadata: dict[str, SeriesMetadata]
    unavailable: list[UnavailableSeries]


@dataclass(frozen=True, slots=True)
class DatasetEvaluation:
    data: SeriesMap
    quality: QualityPayload


@dataclass(frozen=True, slots=True)
class InvalidSeriesRecordError(ValueError):
    reason: str

    def __str__(self) -> str:
        return self.reason


_QUALITY: Final[dict[str, QualityPayload]] = {}
_QUALITY_LOCK: Final[RLock] = RLock()


def _parse_reference(record: Record) -> date:
    raw = record.get("data", record.get("Data"))
    if not isinstance(raw, str) or not raw:
        raise InvalidSeriesRecordError("missing_reference")
    try:
        if "/" in raw:
            day, month, year = raw.split("/")
            return date(int(year), int(month), int(day))
        return date.fromisoformat(raw[:10])
    except ValueError as exc:
        raise InvalidSeriesRecordError("invalid_reference") from exc


def _has_finite_value(record: Record) -> bool:
    candidates = (
        value
        for key, value in record.items()
        if key not in {"data", "Data", "DataReferencia", "dataReferencia"}
    )
    for value in candidates:
        if isinstance(value, bool) or value is None:
            continue
        try:
            if isfinite(float(str(value).replace(",", "."))):
                return True
        except ValueError:
            continue
    return False


def _period_end(reference: date, frequency: Frequency) -> date:
    match frequency:
        case Frequency.DAILY | Frequency.MARKET | Frequency.MOVING_QUARTER:
            return reference
        case Frequency.MONTHLY:
            next_month = reference.replace(day=28) + timedelta(days=4)
            return next_month.replace(day=1) - timedelta(days=1)
        case Frequency.QUARTERLY:
            end_month = ((reference.month - 1) // 3 + 1) * 3
            next_period = date(reference.year + (end_month == 12), end_month % 12 + 1, 1)
            return next_period - timedelta(days=1)
        case Frequency.ANNUAL:
            return date(reference.year, 12, 31)


def evaluate_series_map(
    dataset: str,
    series: Mapping[str, Sequence[Record]],
    required: Sequence[str],
    optional: Sequence[str],
    specs: Mapping[str, FreshnessSpec],
    now: datetime | None = None,
    minimum_usable: int | None = None,
) -> DatasetEvaluation:
    evaluated_at = now or datetime.now(timezone.utc)
    required_set = frozenset(required)
    expected = tuple(dict.fromkeys((*required, *optional)))
    usable: SeriesMap = {}
    metadata: dict[str, SeriesMetadata] = {}
    unavailable: list[UnavailableSeries] = []

    for name in expected:
        records = series.get(name)
        if not isinstance(records, Sequence) or isinstance(records, (str, bytes)):
            unavailable.append({"series": name, "required": name in required_set, "reason": "unexpected_structure"})
            continue
        valid: list[Record] = []
        references: list[date] = []
        for record in records:
            if not _has_finite_value(record):
                continue
            try:
                reference = _parse_reference(record)
            except InvalidSeriesRecordError:
                continue
            valid.append(record)
            references.append(reference)
        spec = specs.get(name)
        if not valid or spec is None:
            reason = "no_valid_observations" if not valid else "missing_spec"
            unavailable.append({"series": name, "required": name in required_set, "reason": reason})
            continue
        latest = max(references)
        deadline = _period_end(latest, spec.frequency) + timedelta(days=spec.freshness_days)
        freshness: Literal["ok", "stale"] = "stale" if evaluated_at.date() > deadline else "ok"
        usable[name] = valid
        metadata[name] = {
            "latest_reference": latest.isoformat(),
            "deadline": deadline.isoformat(),
            "freshness": freshness,
            "observations": len(valid),
        }

    usable_required = sum(name in usable for name in required_set)
    threshold = minimum_usable if minimum_usable is not None else len(required_set)
    if not usable:
        status: DatasetStatus = "unavailable"
    elif usable_required >= threshold:
        status = "ok"
    else:
        status = "partial"
    latest_updated = max((item["latest_reference"] for item in metadata.values()), default=None)
    quality: QualityPayload = {
        "dataset": dataset,
        "status": status,
        "last_updated": latest_updated,
        "metadata": metadata,
        "unavailable": unavailable,
    }
    with _QUALITY_LOCK:
        _QUALITY[dataset] = quality
    return DatasetEvaluation(data=usable, quality=quality)


def get_quality_snapshot(dataset: str) -> QualityPayload | None:
    with _QUALITY_LOCK:
        return _QUALITY.get(dataset)
