import json
from dataclasses import dataclass
from datetime import date, datetime
from hashlib import sha256
from pathlib import Path

import pytest
from pydantic import BaseModel, ConfigDict

from derived.models import DerivedInput, SnapshotOrigin
from macro_score.methodology import COMPONENTS, ComponentSpec, MacroComponentKey
from macro_score.models import ComponentPoint, ComponentSeries

FIXTURE_ROOT = Path(__file__).parent / "fixtures" / "macro_score"
FIXTURE_NAMES = ("2026-08.json", "2026-09.json", "2026-10.json")


class _ExpectedGolden(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    score: float
    classification: str
    overall_coverage: float
    valid_dimensions: int
    dimension_scores: dict[str, float]
    component_score_by_direction: dict[str, float]


class GoldenFixture(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    fixture_id: str
    as_of: datetime
    calculated_at: datetime
    score_month: date
    current_reference_period: date
    current_available_at: datetime
    sample_available_at: datetime
    sample_periods: tuple[date, ...]
    sample_values: tuple[float, ...]
    current_values: dict[str, float]
    expected_payload_sha256: str
    expected: _ExpectedGolden


@dataclass(frozen=True, slots=True)
class _FixturePoint:
    spec_key: MacroComponentKey
    value: float
    reference_period: date
    available_at: datetime
    fixture_id: str
    sample_index: int


def _point(spec: ComponentSpec, seed: _FixturePoint) -> ComponentPoint:
    fingerprint = sha256(
        f"{seed.fixture_id}|{seed.spec_key}|{seed.reference_period}|{seed.value}|{seed.available_at.isoformat()}|{seed.sample_index}".encode()
    ).hexdigest()
    input_value = DerivedInput(
        key="fixture_value",
        value=seed.value,
        unit=spec.unit.value,
        reference_period=seed.reference_period.isoformat(),
        available_at=seed.available_at,
        source=f"golden fixture:{seed.fixture_id}",
        metadata=(("fixture_index", seed.sample_index),),
    )
    return ComponentPoint(
        value=seed.value,
        unit=spec.unit,
        reference_period=seed.reference_period,
        available_at=seed.available_at,
        input_fingerprint=fingerprint,
        inputs=(input_value,),
    )


def _require_score_serialization() -> None:
    path = Path(__file__).parents[1] / "macro_score" / "serialization.py"
    if not path.is_file():
        pytest.fail("[RED XBR-601] Macro Score payload serialization is missing")


def fixture_points(fixture: GoldenFixture) -> tuple[ComponentSeries, ...]:
    series: list[ComponentSeries] = []
    for spec in COMPONENTS:
        key = str(spec.key)
        history = tuple(
            _FixturePoint(
                spec.key,
                value,
                reference_period,
                fixture.sample_available_at,
                fixture.fixture_id,
                index,
            )
            for index, (reference_period, value) in enumerate(
                zip(fixture.sample_periods, fixture.sample_values, strict=True)
            )
        )
        current = _FixturePoint(
            spec.key,
            fixture.current_values[key],
            fixture.current_reference_period,
            fixture.current_available_at,
            fixture.fixture_id,
            len(history),
        )

        series.append(ComponentSeries(
            key=spec.key,
            current=_point(spec, current),
            history=tuple(_point(spec, sample) for sample in history),
            reason=None,
        ))
    return tuple(series)


def load_fixture(name: str) -> GoldenFixture:
    return GoldenFixture.model_validate_json((FIXTURE_ROOT / name).read_text(encoding="utf-8"))


def test_red_xbr_601_three_golden_payloads_are_complete_and_byte_deterministic() -> None:
    _require_score_serialization()
    from macro_score.calculator import aggregate_macro_score
    from macro_score.inputs import score_cutoff
    from macro_score.normalization import normalize_component
    from macro_score.serialization import macro_score_payload

    fixtures = tuple(load_fixture(name) for name in FIXTURE_NAMES)
    assert {fixture.score_month for fixture in fixtures} == {
        date(2026, 8, 1),
        date(2026, 9, 1),
        date(2026, 10, 1),
    }

    for fixture in fixtures:
        cutoff = score_cutoff(fixture.as_of)
        assert cutoff.score_month == fixture.score_month
        first = aggregate_macro_score(
            tuple(normalize_component(item) for item in fixture_points(fixture)),
            cutoff,
            fixture.calculated_at,
        )
        second = aggregate_macro_score(
            tuple(normalize_component(item) for item in fixture_points(fixture)),
            cutoff,
            fixture.calculated_at,
        )
        first_payload = macro_score_payload(first, calculation_cycle_id="golden", origin=SnapshotOrigin.TEST_FIXTURE)
        second_payload = macro_score_payload(second, calculation_cycle_id="golden", origin=SnapshotOrigin.TEST_FIXTURE)

        assert first.score == pytest.approx(fixture.expected.score)
        assert first.classification is not None
        assert first.classification.value == fixture.expected.classification
        assert first.overall_coverage == pytest.approx(fixture.expected.overall_coverage)
        assert first.valid_dimensions == fixture.expected.valid_dimensions
        actual_dimensions = {
            item.dimension.value: item.score
            for item in first.dimensions
            if item.score is not None
        }
        assert actual_dimensions.keys() == fixture.expected.dimension_scores.keys()
        assert all(
            actual_dimensions[key] == pytest.approx(value)
            for key, value in fixture.expected.dimension_scores.items()
        )
        assert all(
            component.score == pytest.approx(
                fixture.expected.component_score_by_direction[component.spec.direction.value]
            )
            for component in first.components
            if component.score is not None
        )
        assert set(first_payload["components"]) == set(fixture.current_values)
        assert all(
            component["value"] == fixture.current_values[key]
            and component["available_at"] == fixture.current_available_at.isoformat()
            and component["methodology_version"] == first.methodology_version
            for key, component in first_payload["components"].items()
        )
        encoded_first = json.dumps(first_payload, sort_keys=True, separators=(",", ":"), allow_nan=False)
        encoded_second = json.dumps(second_payload, sort_keys=True, separators=(",", ":"), allow_nan=False)
        assert encoded_first == encoded_second
        assert sha256(encoded_first.encode("utf-8")).hexdigest() == fixture.expected_payload_sha256
