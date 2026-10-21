from dataclasses import dataclass
from datetime import date, datetime, timezone
from enum import StrEnum, unique
from math import isfinite
from typing import NotRequired, TypeAlias, TypedDict

MetadataValue: TypeAlias = str | int | float | bool | None


@unique
class UnavailableReason(StrEnum):
    INPUT_NOT_AVAILABLE = "input_not_available_at_as_of"
    NON_FINITE_RESULT = "non_finite_result"
    FOCUS_12M_MISSING = "focus_12m_missing"
    DI_CURVE_MISSING = "di_curve_missing"
    DI_CONTRACT_OUTSIDE_TOLERANCE = "di_contract_outside_tolerance"
    REQUIRED_INPUT_MISSING = "required_input_missing"
    REFERENCE_MONTH_MISMATCH = "reference_month_mismatch"
    CURRENT_ACCOUNT_NON_NEGATIVE = "current_account_non_negative"
    PRIMARY_BALANCE_PREVIOUS_MONTH_MISSING = "primary_balance_previous_month_missing"
    INFLATION_TARGET_MISSING = "inflation_target_missing"
    DI_CONTRACT_REUSED = "di_contract_reused"
    INPUT_UNIT_MISMATCH = "input_unit_mismatch"


@unique
class SnapshotOrigin(StrEnum):
    PRODUCTION_REFRESH = "production_refresh"
    TEST_FIXTURE = "test_fixture"


@dataclass(frozen=True, slots=True)
class FutureDerivedInputError(ValueError):
    def __str__(self) -> str:
        return "derived result inputs must be available at as_of"


class DerivedInputPayload(TypedDict):
    value: float
    unit: str
    reference_period: str
    available_at: str
    source: str
    metadata: NotRequired[dict[str, MetadataValue]]


class DerivedResultPayload(TypedDict):
    key: str
    value: float | None
    unit: str
    as_of: str
    methodology_version: str
    formula: str
    inputs: dict[str, DerivedInputPayload]
    reason: str | None
    reference_date: NotRequired[str]
    historical_percentile: NotRequired[float | None]
    classification: NotRequired[str | None]
    classification_reason: NotRequired[str | None]
    classification_history_basis: NotRequired[str]
    classification_history_count: NotRequired[int]
    classification_history_available_at: NotRequired[str]
    dependency_fingerprints: NotRequired[dict[str, str]]


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
    metadata: tuple[tuple[str, MetadataValue], ...] = ()

    def __post_init__(self) -> None:
        if not self.key or not self.unit or not self.reference_period or not self.source:
            raise ValueError("derived input text fields must not be empty")
        if not isfinite(self.value):
            raise ValueError("derived input value must be finite")
        metadata_keys = tuple(key for key, _value in self.metadata)
        if any(not key for key in metadata_keys) or len(metadata_keys) != len(set(metadata_keys)):
            raise ValueError("derived input metadata keys must be non-empty and unique")
        if any(isinstance(value, float) and not isfinite(value) for _key, value in self.metadata):
            raise ValueError("derived input metadata values must be finite")
        object.__setattr__(self, "available_at", utc_datetime(self.available_at))
        object.__setattr__(self, "metadata", tuple(sorted(self.metadata)))


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
    reference_date: str | None = None
    historical_percentile: float | None = None
    classification: str | None = None
    classification_reason: str | None = None
    classification_history_basis: str | None = None
    classification_history_count: int | None = None
    classification_history_available_at: datetime | None = None
    dependency_fingerprints: tuple[tuple[str, str], ...] = ()

    def __post_init__(self) -> None:
        cutoff = utc_datetime(self.as_of)
        if not self.key or not self.unit or not self.methodology_version or not self.formula:
            raise ValueError("derived result text fields must not be empty")
        if self.value is not None and not isfinite(self.value):
            raise ValueError("derived result value must be finite")
        if self.reference_date is not None:
            date.fromisoformat(self.reference_date)
        if self.historical_percentile is not None and (
            not isfinite(self.historical_percentile)
            or not 0 <= self.historical_percentile <= 100
        ):
            raise ValueError("historical percentile must be finite and between 0 and 100")
        if (self.historical_percentile is None) != (self.classification is None):
            raise ValueError("classification and historical percentile must be set together")
        if self.classification_history_count is not None and self.classification_history_count < 0:
            raise ValueError("classification history count must not be negative")
        if (self.classification_history_count is None) != (self.classification_history_basis is None):
            raise ValueError("classification history count and basis must be set together")
        if self.classification_history_available_at is not None:
            object.__setattr__(
                self,
                "classification_history_available_at",
                utc_datetime(self.classification_history_available_at),
            )
        if (self.value is None) == (self.reason is None):
            raise ValueError("derived result must contain either a value or an unavailable reason")
        keys = tuple(item.key for item in self.inputs)
        if len(keys) != len(set(keys)):
            raise ValueError("derived input keys must be unique")
        dependency_keys = tuple(key for key, _fingerprint in self.dependency_fingerprints)
        if (
            any(not key or not fingerprint for key, fingerprint in self.dependency_fingerprints)
            or len(dependency_keys) != len(set(dependency_keys))
        ):
            raise ValueError("dependency fingerprints must be non-empty and unique")
        if any(item.available_at > cutoff for item in self.inputs):
            raise FutureDerivedInputError
        object.__setattr__(self, "as_of", cutoff)
        object.__setattr__(self, "inputs", tuple(sorted(self.inputs, key=lambda item: item.key)))
        object.__setattr__(self, "dependency_fingerprints", tuple(sorted(self.dependency_fingerprints)))


def result_to_payload(
    result: DerivedResult,
    *,
    include_dependencies: bool = False,
) -> DerivedResultPayload:
    inputs: dict[str, DerivedInputPayload] = {}
    for item in result.inputs:
        input_payload: DerivedInputPayload = {
            "value": item.value,
            "unit": item.unit,
            "reference_period": item.reference_period,
            "available_at": item.available_at.isoformat(),
            "source": item.source,
        }
        if item.metadata:
            input_payload["metadata"] = dict(item.metadata)
        inputs[item.key] = input_payload
    payload: DerivedResultPayload = {
        "key": result.key,
        "value": result.value,
        "unit": result.unit,
        "as_of": result.as_of.isoformat(),
        "methodology_version": result.methodology_version,
        "formula": result.formula,
        "inputs": inputs,
        "reason": result.reason.value if result.reason is not None else None,
    }
    if result.reference_date is not None:
        payload["reference_date"] = result.reference_date
    if result.reference_date is not None or result.classification_reason is not None:
        payload["historical_percentile"] = result.historical_percentile
        payload["classification"] = result.classification
        payload["classification_reason"] = result.classification_reason
    if result.classification_history_basis is not None:
        payload["classification_history_basis"] = result.classification_history_basis
    if result.classification_history_count is not None:
        payload["classification_history_count"] = result.classification_history_count
    if result.classification_history_available_at is not None:
        payload["classification_history_available_at"] = result.classification_history_available_at.isoformat()
    if include_dependencies and result.dependency_fingerprints:
        payload["dependency_fingerprints"] = dict(result.dependency_fingerprints)
    return payload


def result_from_payload(payload: DerivedResultPayload) -> DerivedResult:
    reason = payload["reason"]
    history_available_at = payload.get("classification_history_available_at")
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
                metadata=tuple(value.get("metadata", {}).items()),
            )
            for key, value in payload["inputs"].items()
        ),
        reason=UnavailableReason(reason) if reason is not None else None,
        reference_date=payload.get("reference_date"),
        historical_percentile=payload.get("historical_percentile"),
        classification=payload.get("classification"),
        classification_reason=payload.get("classification_reason"),
        classification_history_basis=payload.get("classification_history_basis"),
        classification_history_count=payload.get("classification_history_count"),
        classification_history_available_at=(
            datetime.fromisoformat(history_available_at)
            if history_available_at is not None
            else None
        ),
        dependency_fingerprints=tuple(payload.get("dependency_fingerprints", {}).items()),
    )
