from datetime import date, datetime, timedelta, timezone
from pathlib import Path

import pytest

from macro_score.methodology import (
    COMPONENTS,
    COMPONENTS_BY_KEY,
    MacroComponentKey,
)
from macro_score.models import ComponentPoint, ComponentUnavailableReason
from macro_score.normalization import NormalizedComponent


def _require_calculator_module() -> None:
    calculator_path = Path(__file__).parents[1] / "macro_score" / "calculator.py"
    if not calculator_path.is_file():
        pytest.fail("[RED XBR-601] Macro Score coverage aggregation is missing")


def _component(key: str, score: float | None) -> NormalizedComponent:
    spec = COMPONENTS_BY_KEY[MacroComponentKey(key)]
    return NormalizedComponent(
        spec=spec,
        current=ComponentPoint(
            value=55.0,
            unit=spec.unit,
            reference_period=date(2026, 8, 31),
            available_at=datetime(2026, 9, 1, tzinfo=timezone.utc),
            input_fingerprint=f"{key}-current",
            inputs=(),
        ),
        percentile=score,
        score=score,
        reason=None if score is not None else ComponentUnavailableReason.INSUFFICIENT_HISTORY,
        history_count=24 if score is not None else 23,
        history_fingerprint=f"{key}-history",
    )


def test_red_xbr_601_coverage_accepts_exact_two_thirds_and_requires_five_dimensions() -> None:
    _require_calculator_module()
    from macro_score.calculator import aggregate_macro_score
    from macro_score.inputs import score_cutoff

    invalid_keys = {
        "ibc_br_sa_yoy",
        "abs_ipca_12m_target",
        "saldo_nominal_12m_pib",
        "inadimplencia_pf",
        "reservas_internacionais",
    }
    components = tuple(
        _component(str(spec.key), None if str(spec.key) in invalid_keys else 50.0)
        for spec in COMPONENTS
    )
    as_of = datetime(2026, 9, 27, 12, tzinfo=timezone.utc)
    calculated_at = as_of + timedelta(seconds=1)

    result = aggregate_macro_score(components, score_cutoff(as_of), calculated_at)

    assert result.score == 50.0
    assert result.valid_dimensions == 5
    assert result.overall_coverage == pytest.approx(5 / 6)
    activity = next(item for item in result.dimensions if item.dimension.value == "activity")
    assert activity.valid_components == 2
    assert activity.coverage == pytest.approx(2 / 3)
    assert activity.score == 50.0


def test_macro_score_is_unavailable_with_only_four_dimensions() -> None:
    _require_calculator_module()
    from macro_score.calculator import aggregate_macro_score
    from macro_score.inputs import score_cutoff

    invalid_dimensions = {"activity", "inflation"}
    components = tuple(
        _component(
            str(spec.key),
            None if spec.dimension.value in invalid_dimensions else 50.0,
        )
        for spec in COMPONENTS
    )
    as_of = datetime(2026, 9, 27, 12, tzinfo=timezone.utc)

    result = aggregate_macro_score(components, score_cutoff(as_of), as_of)

    assert result.valid_dimensions == 4
    assert result.score is None
    assert result.classification is None
    assert result.overall_coverage == pytest.approx(4 / 6)


@pytest.mark.parametrize(
    ("value", "classification"),
    [
        (0.0, "muito_fraco"),
        (19.999, "muito_fraco"),
        (20.0, "fraco"),
        (40.0, "neutro"),
        (60.0, "forte"),
        (80.0, "muito_forte"),
        (100.0, "muito_forte"),
    ],
)
def test_classification_uses_roadmap_half_open_bands(
    value: float,
    classification: str,
) -> None:
    _require_calculator_module()
    from macro_score.calculator import classify_score

    assert classify_score(value).value == classification
