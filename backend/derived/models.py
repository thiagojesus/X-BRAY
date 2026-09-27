from dataclasses import dataclass
from datetime import datetime, timezone
from enum import StrEnum, unique
from math import isfinite
from typing import TypedDict


@unique
class UnavailableReason(StrEnum):
    INPUT_NOT_AVAILABLE = "input_not_available_at_as_of"
    NON_FINITE_RESULT = "non_finite_result"


@unique
class SnapshotOrigin(StrEnum):
    PRODUCTION_REFRESH = "production_refresh"
    TEST_FIXTURE = "test_fixture"


class DerivedInputPayload(TypedDict):
    value: float
    unit: str
    reference_period: str
    available_at: str
    source: str


class DerivedResultPayload(TypedDict):
    key: str
    value: float | None
    unit: str
    as_of: str
    methodology_version: str
    formula: str
    inputs: dict[str, DerivedInputPayload]
    reason: str | None


def utc_datetime(value: datetime) -> datetime:
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError("timestamp must include a timezone")
    return value.astimezone(timezone.utc)


@dataclass(frozen=True, slots=True)
class DerivedInput:
    key: str
    value: float
    unit: str
    reference_period: str
    available_at: datetime
    source: str

    def __post_init__(self) -> None:
        if not self.key or not self.unit or not self.reference_period or not self.source:
            raise ValueError("derived input text fields must not be empty")
        if not isfinite(self.value):
            raise ValueError("derived input value must be finite")
        object.__setattr__(self, "available_at", utc_datetime(self.available_at))


@dataclass(frozen=True, slots=True)
class DerivedResult:
    key: str
    value: float | None
    unit: str
    as_of: datetime
    methodology_version: str
    formula: str
    inputs: tuple[DerivedInput, ...]
    reason: UnavailableReason | None

    def __post_init__(self) -> None:
        if not self.key or not self.unit or not self.methodology_version or not self.formula:
            raise ValueError("derived result text fields must not be empty")
        if self.value is not None and not isfinite(self.value):
            raise ValueError("derived result value must be finite")
        if (self.value is None) == (self.reason is None):
            raise ValueError("derived result must contain either a value or an unavailable reason")
        keys = tuple(item.key for item in self.inputs)
        if len(keys) != len(set(keys)):
            raise ValueError("derived input keys must be unique")
        object.__setattr__(self, "as_of", utc_datetime(self.as_of))
        object.__setattr__(self, "inputs", tuple(sorted(self.inputs, key=lambda item: item.key)))


def result_to_payload(result: DerivedResult) -> DerivedResultPayload:
    return {
        "key": result.key,
        "value": result.value,
        "unit": result.unit,
        "as_of": result.as_of.isoformat(),
        "methodology_version": result.methodology_version,
        "formula": result.formula,
        "inputs": {
            item.key: {
                "value": item.value,
                "unit": item.unit,
                "reference_period": item.reference_period,
                "available_at": item.available_at.isoformat(),
                "source": item.source,
            }
            for item in result.inputs
        },
        "reason": result.reason.value if result.reason is not None else None,
    }


def result_from_payload(payload: DerivedResultPayload) -> DerivedResult:
    reason = payload["reason"]
    return DerivedResult(
        key=payload["key"],
        value=payload["value"],
        unit=payload["unit"],
        as_of=datetime.fromisoformat(payload["as_of"]),
        methodology_version=payload["methodology_version"],
        formula=payload["formula"],
        inputs=tuple(
            DerivedInput(
                key=key,
                value=value["value"],
                unit=value["unit"],
                reference_period=value["reference_period"],
                available_at=datetime.fromisoformat(value["available_at"]),
                source=value["source"],
            )
            for key, value in payload["inputs"].items()
        ),
        reason=UnavailableReason(reason) if reason is not None else None,
    )
