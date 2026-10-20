from typing import NotRequired, TypedDict

from derived.models import DerivedInput, MetadataValue, SnapshotOrigin
from macro_score.calculator import MacroScoreResult


class ScoreInputPayload(TypedDict):
    value: float
    unit: str
    reference_period: str
    available_at: str
    source: str
    metadata: dict[str, MetadataValue]


class ScoreComponentPayload(TypedDict):
    key: str
    dimension: str
    value: float | None
    unit: str
    reference_period: str | None
    available_at: str | None
    methodology_version: str
    formula: str
    direction: str
    weight: int
    percentile: float | None
    score: float | None
    reason: str | None
    history_count: int
    history_fingerprint: str
    history_basis: NotRequired[str]
    inputs: dict[str, ScoreInputPayload]


class ScoreDimensionPayload(TypedDict):
    score: float | None
    valid_components: int
    total_components: int
    coverage: float
    reason: str | None
    components: list[str]


class MacroScorePayload(TypedDict):
    score: float | None
    classification: str | None
    calculated_at: str
    as_of: str
    score_month: str
    methodology_version: str
    overall_coverage: float
    valid_dimensions: int
    total_dimensions: int
    dimensions: dict[str, ScoreDimensionPayload]
    components: dict[str, ScoreComponentPayload]
    warnings: list[str]
    inputs_hash: str
    calculation_cycle_id: str
    origin: str


def _input_payload(item: DerivedInput) -> ScoreInputPayload:
    return {
        "value": item.value,
        "unit": item.unit,
        "reference_period": item.reference_period,
        "available_at": item.available_at.isoformat(),
        "source": item.source,
        "metadata": dict(item.metadata),
    }


def macro_score_payload(
    result: MacroScoreResult,
    *,
    calculation_cycle_id: str,
    origin: SnapshotOrigin,
) -> MacroScorePayload:
    components: dict[str, ScoreComponentPayload] = {}
    for component in result.components:
        spec = component.spec
        current = component.current
        components[str(spec.key)] = {
            "key": str(spec.key),
            "dimension": spec.dimension.value,
            "value": current.value if current is not None else None,
            "unit": spec.unit.value,
            "reference_period": (
                current.reference_period.isoformat() if current is not None else None
            ),
            "available_at": current.available_at.isoformat()
            if current is not None
            else None,
            "methodology_version": result.methodology_version,
            "formula": spec.formula,
            "direction": spec.direction.value,
            "weight": spec.weight,
            "percentile": component.percentile,
            "score": component.score,
            "reason": component.reason.value if component.reason is not None else None,
            "history_count": component.history_count,
            "history_fingerprint": component.history_fingerprint,
            "inputs": {item.key: _input_payload(item) for item in current.inputs}
            if current is not None
            else {},
        }
        if component.history_basis == "retrospective_reference":
            components[str(spec.key)]["history_basis"] = "retrospective_reference"

    dimensions: dict[str, ScoreDimensionPayload] = {
        dimension.dimension.value: {
            "score": dimension.score,
            "valid_components": dimension.valid_components,
            "total_components": dimension.total_components,
            "coverage": dimension.coverage,
            "reason": dimension.reason.value if dimension.reason is not None else None,
            "components": [
                str(component.spec.key) for component in dimension.components
            ],
        }
        for dimension in result.dimensions
    }
    return {
        "score": result.score,
        "classification": result.classification.value
        if result.classification is not None
        else None,
        "calculated_at": result.calculated_at.isoformat(),
        "as_of": result.as_of.isoformat(),
        "score_month": result.score_month.strftime("%Y-%m"),
        "methodology_version": result.methodology_version,
        "overall_coverage": result.overall_coverage,
        "valid_dimensions": result.valid_dimensions,
        "total_dimensions": result.total_dimensions,
        "dimensions": dimensions,
        "components": components,
        "warnings": list(result.warnings),
        "inputs_hash": result.input_fingerprint,
        "calculation_cycle_id": calculation_cycle_id,
        "origin": origin.value,
    }
