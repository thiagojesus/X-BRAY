import hashlib
import json
from dataclasses import dataclass
from datetime import date, datetime
from math import isfinite

from derived.models import DerivedInput, utc_datetime
from macro_score.methodology import MACRO_SCORE_VERSION, ComponentSpec
from macro_score.models import ComponentPoint
from macro_score.sources import ScoreVintage


@dataclass(frozen=True, slots=True)
class ComponentCalculation:
    value: float
    reference_period: date
    available_at: datetime
    inputs: tuple[DerivedInput, ...]

    def __post_init__(self) -> None:
        if not isfinite(self.value) or not self.inputs:
            raise ValueError("component calculations require a finite value and source inputs")
        object.__setattr__(self, "available_at", utc_datetime(self.available_at))


def score_vintage_input(key: str, vintage: ScoreVintage) -> DerivedInput:
    return DerivedInput(
        key=key,
        value=vintage.value,
        unit=vintage.unit,
        reference_period=vintage.reference_period.isoformat(),
        available_at=vintage.available_at,
        source=vintage.source,
        metadata=(
            ("availability_basis", vintage.availability_basis),
            ("first_seen_at", vintage.first_seen_at.isoformat()),
            ("payload_hash", vintage.payload_hash),
            (
                "source_published_at",
                vintage.source_published_at.isoformat()
                if vintage.source_published_at is not None
                else None,
            ),
        ),
    )


def calculation_from_inputs(
    value: float,
    inputs: tuple[DerivedInput, ...],
) -> ComponentCalculation:
    return ComponentCalculation(
        value=value,
        reference_period=date.fromisoformat(inputs[0].reference_period),
        available_at=max(item.available_at for item in inputs),
        inputs=inputs,
    )


def component_point(
    spec: ComponentSpec,
    calculation: ComponentCalculation,
) -> ComponentPoint:
    inputs = calculation.inputs
    reference_period = calculation.reference_period
    payload = {
        "component": str(spec.key),
        "formula": spec.formula,
        "inputs": [
            {
                "available_at": item.available_at.isoformat(),
                "key": item.key,
                "metadata": dict(item.metadata),
                "reference_period": item.reference_period,
                "source": item.source,
                "unit": item.unit,
                "value": item.value,
            }
            for item in inputs
        ],
        "methodology_version": MACRO_SCORE_VERSION,
        "reference_period": reference_period.isoformat(),
        "value": calculation.value,
    }
    fingerprint = hashlib.sha256(
        json.dumps(
            payload,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
            allow_nan=False,
        ).encode("utf-8")
    ).hexdigest()
    return ComponentPoint(
        value=calculation.value,
        unit=spec.unit,
        reference_period=reference_period,
        available_at=calculation.available_at,
        input_fingerprint=fingerprint,
        inputs=inputs,
    )
