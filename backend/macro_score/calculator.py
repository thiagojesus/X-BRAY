import hashlib
import json
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import date, datetime
from enum import StrEnum, unique
from math import fsum, isfinite
from typing import Final

from derived.models import utc_datetime
from macro_score.inputs import MacroScoreCutoff
from macro_score.methodology import (
    COMPONENTS,
    DIMENSION_MINIMUM_VALID,
    MACRO_SCORE_VERSION,
    MINIMUM_VALID_DIMENSIONS,
    Dimension,
)
from macro_score.normalization import NormalizedComponent

TOTAL_DIMENSIONS: Final = len(Dimension)


@unique
class MacroScoreClassification(StrEnum):
    VERY_WEAK = "muito_fraco"
    WEAK = "fraco"
    NEUTRAL = "neutro"
    STRONG = "forte"
    VERY_STRONG = "muito_forte"


@unique
class DimensionUnavailableReason(StrEnum):
    INSUFFICIENT_COVERAGE = "insufficient_coverage"


@dataclass(frozen=True, slots=True)
class MacroScoreClockError(ValueError):
    as_of: datetime
    calculated_at: datetime

    def __str__(self) -> str:
        return "macro score completion must not precede its information cutoff"


@dataclass(frozen=True, slots=True)
class MacroScoreInventoryError(ValueError):
    missing: tuple[str, ...]
    extra: tuple[str, ...]
    duplicates: tuple[str, ...]

    def __str__(self) -> str:
        return (
            "macro score component inventory mismatch: "
            f"missing={self.missing}, extra={self.extra}, duplicates={self.duplicates}"
        )


@dataclass(frozen=True, slots=True)
class InvalidMacroScoreError(ValueError):
    score: float

    def __str__(self) -> str:
        return "macro score must be finite and within [0, 100]"


@dataclass(frozen=True, slots=True)
class DimensionScore:
    dimension: Dimension
    score: float | None
    valid_components: int
    total_components: int
    coverage: float
    reason: DimensionUnavailableReason | None
    components: tuple[NormalizedComponent, ...]


@dataclass(frozen=True, slots=True)
class MacroScoreResult:
    score: float | None
    classification: MacroScoreClassification | None
    calculated_at: datetime
    as_of: datetime
    score_month: date
    methodology_version: str
    overall_coverage: float
    valid_dimensions: int
    total_dimensions: int
    dimensions: tuple[DimensionScore, ...]
    components: tuple[NormalizedComponent, ...]
    input_fingerprint: str
    warnings: tuple[str, ...]


def classify_score(score: float) -> MacroScoreClassification:
    if not isfinite(score) or not 0 <= score <= 100:
        raise InvalidMacroScoreError(score)
    if score < 20:
        return MacroScoreClassification.VERY_WEAK
    if score < 40:
        return MacroScoreClassification.WEAK
    if score < 60:
        return MacroScoreClassification.NEUTRAL
    if score < 80:
        return MacroScoreClassification.STRONG
    return MacroScoreClassification.VERY_STRONG


def _validate_inventory(components: Sequence[NormalizedComponent]) -> None:
    expected = {spec.key for spec in COMPONENTS}
    actual = tuple(component.spec.key for component in components)
    actual_set = set(actual)
    missing = tuple(sorted(str(key) for key in expected - actual_set))
    extra = tuple(sorted(str(key) for key in actual_set - expected))
    duplicates = tuple(sorted({str(key) for key in actual if actual.count(key) > 1}))
    if missing or extra or duplicates:
        raise MacroScoreInventoryError(missing, extra, duplicates)


def _input_fingerprint(
    components: tuple[NormalizedComponent, ...],
    cutoff: MacroScoreCutoff,
) -> str:
    payload = {
        "as_of": cutoff.as_of.isoformat(),
        "components": [
            {
                "available_at": (
                    component.current.available_at.isoformat()
                    if component.current is not None
                    else None
                ),
                "current_fingerprint": (
                    component.current.input_fingerprint
                    if component.current is not None
                    else None
                ),
                "direction": component.spec.direction.value,
                "formula": component.spec.formula,
                "history_count": component.history_count,
                "history_fingerprint": component.history_fingerprint,
                "key": str(component.spec.key),
                "methodology_version": MACRO_SCORE_VERSION,
                "reason": component.reason.value if component.reason is not None else None,
                "reference_period": (
                    component.current.reference_period.isoformat()
                    if component.current is not None
                    else None
                ),
                "unit": component.spec.unit.value,
                "value": component.current.value if component.current is not None else None,
                "weight": component.spec.weight,
            }
            for component in components
        ],
        "score_month": cutoff.score_month.isoformat(),
    }
    return hashlib.sha256(
        json.dumps(
            payload,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
            allow_nan=False,
        ).encode("utf-8")
    ).hexdigest()


def _dimension_scores(
    components: tuple[NormalizedComponent, ...],
) -> tuple[DimensionScore, ...]:
    results: list[DimensionScore] = []
    for dimension in Dimension:
        members = tuple(
            component
            for component in components
            if component.spec.dimension is dimension
        )
        valid = tuple(
            component
            for component in members
            if component.score is not None and component.reason is None
        )
        total = len(members)
        count = len(valid)
        coverage = count / total
        reason = (
            None
            if count >= DIMENSION_MINIMUM_VALID[dimension]
            else DimensionUnavailableReason.INSUFFICIENT_COVERAGE
        )
        score = fsum(component.score for component in valid if component.score is not None) / count if reason is None else None
        results.append(DimensionScore(dimension, score, count, total, coverage, reason, members))
    return tuple(results)


def aggregate_macro_score(
    components: Sequence[NormalizedComponent],
    cutoff: MacroScoreCutoff,
    calculated_at: datetime,
) -> MacroScoreResult:
    _validate_inventory(components)
    ordered = tuple(
        next(component for component in components if component.spec.key == spec.key)
        for spec in COMPONENTS
    )
    completed = utc_datetime(calculated_at)
    if completed < cutoff.as_of:
        raise MacroScoreClockError(cutoff.as_of, completed)
    dimensions = _dimension_scores(ordered)
    valid_dimensions = tuple(item for item in dimensions if item.score is not None)
    score = (
        fsum(item.score for item in valid_dimensions if item.score is not None) / len(valid_dimensions)
        if len(valid_dimensions) >= MINIMUM_VALID_DIMENSIONS
        else None
    )
    classification = classify_score(score) if score is not None else None
    warnings = tuple(
        f"{item.spec.key}:{item.reason.value}"
        for item in ordered
        if item.reason is not None
    ) + tuple(
        f"{item.dimension.value}:{item.reason.value}"
        for item in dimensions
        if item.reason is not None
    )
    return MacroScoreResult(
        score=score,
        classification=classification,
        calculated_at=completed,
        as_of=cutoff.as_of,
        score_month=cutoff.score_month,
        methodology_version=MACRO_SCORE_VERSION,
        overall_coverage=len(valid_dimensions) / TOTAL_DIMENSIONS,
        valid_dimensions=len(valid_dimensions),
        total_dimensions=TOTAL_DIMENSIONS,
        dimensions=dimensions,
        components=ordered,
        input_fingerprint=_input_fingerprint(ordered, cutoff),
        warnings=warnings,
    )
