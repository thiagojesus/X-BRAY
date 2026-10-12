from dataclasses import dataclass
from datetime import date, datetime

from pydantic import BaseModel, ConfigDict, Field, ValidationError

from db import store
from derived.models import utc_datetime
from indicator_catalog import (
    INDICATOR_CATALOG,
    Frequency,
    IndicatorKey,
    IndicatorSpec,
    ProviderCode,
    Transformation,
    Unit,
)


@dataclass(frozen=True, slots=True)
class SGSObservation:
    indicator_key: IndicatorKey
    provider_code: ProviderCode
    value: float
    unit: Unit
    frequency: Frequency
    reference_period: date
    source: str
    available_at: datetime
    availability_basis: str
    source_published_at: datetime | None
    first_seen_at: datetime


class _SGSRawValue(BaseModel):
    model_config = ConfigDict(frozen=True, extra="ignore")

    valor: float = Field(allow_inf_nan=False)


def _raw_number(value: object) -> float | None:
    try:
        parsed = _SGSRawValue.model_validate_json(str(value))
    except ValidationError:
        return None
    return parsed.valor


def _timestamp(value: object) -> datetime | None:
    if not isinstance(value, str):
        return None
    try:
        return utc_datetime(datetime.fromisoformat(value.replace("Z", "+00:00")))
    except ValueError:
        return None


def _observation(
    row: dict[str, object], spec: IndicatorSpec
) -> SGSObservation | None:
    try:
        reference_period = date.fromisoformat(str(row["reference_period"])[:10])
    except ValueError:
        return None
    number = _raw_number(row["raw_value_json"])
    available_at = _timestamp(row["available_at"])
    source_published_at = _timestamp(row["source_published_at"])
    first_seen_at = _timestamp(row["first_seen_at"])
    if number is None or available_at is None or first_seen_at is None:
        return None
    if spec.transformation is Transformation.INVERT_SIGN:
        number = -number
    return SGSObservation(
        indicator_key=spec.key,
        provider_code=spec.provider_code,
        value=number,
        unit=spec.unit,
        frequency=spec.frequency,
        reference_period=reference_period,
        source=spec.source_url,
        available_at=available_at,
        availability_basis=(
            "source_published_at" if source_published_at is not None else "first_seen_at"
        ),
        source_published_at=source_published_at,
        first_seen_at=first_seen_at,
    )


def load_sgs_observations(
    indicator_key: IndicatorKey,
    *,
    as_of: datetime,
) -> tuple[SGSObservation, ...]:
    spec = INDICATOR_CATALOG[indicator_key]
    cutoff = utc_datetime(as_of)
    rows = store.query_rows(
        "SELECT reference_period, raw_value_json, source_published_at, first_seen_at, "
        "available_at FROM observation_vintages WHERE provider = ? AND series_key = ? "
        "AND available_at <= ? ORDER BY reference_period, available_at, first_seen_at, payload_hash",
        (spec.provider.value, str(spec.key), cutoff.isoformat()),
    )
    observations = (_observation(row, spec) for row in rows)
    return tuple(item for item in observations if item is not None)
