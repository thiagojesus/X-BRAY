from __future__ import annotations

import json
from datetime import datetime
from math import isclose, isfinite
from typing import TYPE_CHECKING, Any, Final, cast

from datafetchers.focus import FOCUS_12M_FILTER, FOCUS_12M_KEY
from derived.models import DerivedInput, DerivedResult, utc_datetime
from derived.real_rate import FORMULA, METHODOLOGY_VERSION, REAL_RATE_KEY
from derived.real_rate_inputs import parse_reference_date

if TYPE_CHECKING:
    from macro_score.sources import DIInstrumentSnapshot

_HISTORY_BASIS: Final = "retrospective_reference"


def _timestamp(value: object) -> datetime | None:
    if not isinstance(value, str):
        return None
    try:
        return utc_datetime(datetime.fromisoformat(value.replace("Z", "+00:00")))
    except ValueError:
        return None


def _record(value: object) -> dict[str, object] | None:
    if not isinstance(value, dict):
        return None
    return cast(dict[str, object], value)


def _as_result(
    row: tuple[object, ...], *, cutoff: datetime, registry: tuple[DIInstrumentSnapshot, ...]
) -> DerivedResult | None:
    from macro_score.retrospective import DI_FORMULA_VERSION, has_verified_di_maturities

    if (
        len(row) != 10
        or row[0] != REAL_RATE_KEY
        or row[3] != "pct_annual"
        or row[4] != DI_FORMULA_VERSION
        or row[5] != _HISTORY_BASIS
    ):
        return None
    try:
        inputs_json = str(row[7])
        inputs = json.loads(inputs_json)
    except (TypeError, ValueError, json.JSONDecodeError):
        return None
    raw_value = row[2]
    if (
        not isinstance(inputs, dict)
        or isinstance(raw_value, bool)
        or not isinstance(raw_value, (int, float))
    ):
        return None
    value = float(raw_value)
    if not isfinite(value):
        return None
    inputs = cast(dict[str, Any], inputs)
    if not has_verified_di_maturities(inputs_json, registry=registry):
        return None
    reference_date = parse_reference_date(inputs.get("reference_date"))
    month = row[1]
    captured_at = _timestamp(row[6])
    if (
        reference_date is None
        or not isinstance(month, str)
        or reference_date.strftime("%Y-%m") != month
        or captured_at is None
        or captured_at > cutoff
    ):
        return None
    di = _record(inputs.get("di_1y"))
    focus = _record(inputs.get("focus_12m_base0"))
    if di is None or focus is None:
        return None
    di_rate = di.get("rate")
    focus_value = focus.get("value")
    di_available = _timestamp(di.get("fetched_at"))
    focus_available = _timestamp(focus.get("available_at"))
    di_date = di.get("trade_date")
    focus_date = focus.get("reference_date")
    if (
        isinstance(di_rate, bool)
        or not isinstance(di_rate, (int, float))
        or not isfinite(float(di_rate))
        or isinstance(focus_value, bool)
        or not isinstance(focus_value, (int, float))
        or not isfinite(float(focus_value))
        or di_available is None
        or focus_available is None
        or di_available > cutoff
        or focus_available > cutoff
        or not isinstance(di_date, str)
        or not isinstance(focus_date, str)
    ):
        return None
    derived_inputs = (
        DerivedInput(
            key="nominal_rate",
            value=float(di_rate),
            unit="pct_annual",
            reference_period=di_date,
            available_at=di_available,
            source="B3 DI retrospective_reference",
            metadata=(
                ("availability_basis", _HISTORY_BASIS),
                ("source_formula_version", DI_FORMULA_VERSION),
                ("maturity", str(di.get("maturity", ""))),
                ("symbol", str(di.get("symbol", ""))),
            ),
        ),
        DerivedInput(
            key="expected_inflation",
            value=float(focus_value),
            unit="pct_annual",
            reference_period=focus_date,
            available_at=focus_available,
            source="Focus IPCA 12m retrospective_reference",
            metadata=(
                ("availability_basis", _HISTORY_BASIS),
                ("filter", FOCUS_12M_FILTER),
                ("series_key", FOCUS_12M_KEY),
                ("source_formula_version", DI_FORMULA_VERSION),
            ),
        ),
    )
    denominator = 1 + float(focus_value) / 100
    if denominator == 0:
        return None
    computed = ((1 + float(di_rate) / 100) / denominator - 1) * 100
    if not isclose(computed, value, rel_tol=1e-12, abs_tol=1e-12):
        return None
    return DerivedResult(
        key=REAL_RATE_KEY,
        value=value,
        unit="pct_annual",
        as_of=captured_at,
        methodology_version=METHODOLOGY_VERSION,
        formula=FORMULA,
        inputs=derived_inputs,
        reason=None,
        reference_date=reference_date.isoformat(),
        dependency_fingerprints=(("retrospective_source", str(row[8])),),
    )


def load_retrospective_real_rate_history(*, as_of: datetime) -> tuple[DerivedResult, ...]:
    """Build verified monthly Fisher reference values from already-imported ledgers."""
    from macro_score.retrospective import build_retrospective_di_values
    from macro_score.sources import (
        MacroScoreSources,
        load_di_instrument_snapshots,
        load_retrospective_di_observations,
        load_score_vintages,
    )

    cutoff = utc_datetime(as_of)
    source = MacroScoreSources(
        vintages=load_score_vintages(as_of=cutoff),
        target_observations=(),
        derived_snapshots=(),
        retrospective_di=load_retrospective_di_observations(as_of=cutoff),
        di_instrument_snapshots=load_di_instrument_snapshots(as_of=cutoff),
    )
    rows, _failures = build_retrospective_di_values(source)
    return tuple(
        result
        for row in rows
        if (result := _as_result(row, cutoff=cutoff, registry=source.di_instrument_snapshots))
        is not None
    )
