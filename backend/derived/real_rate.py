import hashlib
import json
from collections.abc import Sequence
from dataclasses import replace
from datetime import date, datetime, time, timezone
from typing import Final

from datafetchers.focus import FOCUS_12M_FILTER
from derived.di_selection import select_di_contract_for_horizon
from derived.engine import DerivationSpec, calculate
from derived.models import DerivedInput, DerivedResult, UnavailableReason, utc_datetime
from derived.percentiles import (
    PercentileBand,
    PercentileObservation,
    eligible_observations,
    empirical_percentile,
)
from derived.real_rate_inputs import (
    DIContract,
    FocusExpectation,
    load_di_contracts,
    load_focus_expectations,
    parse_reference_date,
)

REAL_RATE_KEY: Final = "juro_real_ex_ante_1y"
METHODOLOGY_VERSION: Final = "1.0.0"
FORMULA: Final = "((1 + DI/100) / (1 + IPCA/100) - 1) * 100"

_CLASSIFICATIONS: Final = {
    PercentileBand.VERY_LOW: "muito_baixo",
    PercentileBand.LOW: "baixo",
    PercentileBand.NEUTRAL: "neutro",
    PercentileBand.HIGH: "restritivo",
    PercentileBand.VERY_HIGH: "muito_restritivo",
}
_PRODUCTION_HISTORY_BASIS: Final = "production_forward_captured"
_RETROSPECTIVE_HISTORY_BASIS: Final = (
    "retrospective_reference_monthly_plus_production_forward_captured_monthly"
)


def select_di_contract(
    contracts: Sequence[DIContract],
    *,
    reference_date: date,
) -> DIContract | None:
    return select_di_contract_for_horizon(contracts, reference_date, 365)


def _fisher(inputs: tuple[DerivedInput, ...]) -> float:
    values = {item.key: item.value for item in inputs}
    denominator = 1 + values["expected_inflation"] / 100
    if denominator == 0:
        return float("nan")
    return ((1 + values["nominal_rate"] / 100) / denominator - 1) * 100


_SPEC: Final = DerivationSpec(
    key=REAL_RATE_KEY,
    unit="pct_annual",
    methodology_version=METHODOLOGY_VERSION,
    formula=FORMULA,
    calculate=_fisher,
)


def _unavailable(
    *,
    as_of: datetime,
    reason: UnavailableReason,
    reference_date: date | None = None,
    inputs: tuple[DerivedInput, ...] = (),
) -> DerivedResult:
    return DerivedResult(
        key=REAL_RATE_KEY,
        value=None,
        unit="pct_annual",
        as_of=as_of,
        methodology_version=METHODOLOGY_VERSION,
        formula=FORMULA,
        inputs=inputs,
        reason=reason,
        reference_date=reference_date.isoformat() if reference_date is not None else None,
    )


def _focus_input(expectation: FocusExpectation) -> DerivedInput:
    return DerivedInput(
        key="expected_inflation",
        value=expectation.median,
        unit="pct_annual",
        reference_period=expectation.reference_date.isoformat(),
        available_at=expectation.available_at,
        source="Focus IPCA 12m",
        metadata=(
            ("availability_basis", expectation.availability_basis),
            ("filter", FOCUS_12M_FILTER),
        ),
    )


def _di_input(contract: DIContract) -> DerivedInput:
    return DerivedInput(
        key="nominal_rate",
        value=contract.rate,
        unit="pct_annual",
        reference_period=contract.trade_date.isoformat(),
        available_at=contract.available_at,
        source="B3 DI",
        metadata=(
            ("availability_basis", contract.availability_basis),
            ("maturity", contract.maturity.isoformat()),
            ("symbol", contract.symbol),
        ),
    )


def _historical_observations(
    history: Sequence[DerivedResult],
    current: DerivedResult,
    retrospective_history: Sequence[DerivedResult] = (),
) -> tuple[tuple[PercentileObservation, ...], bool, datetime | None]:
    cutoff = utc_datetime(current.as_of)
    eligible_retrospective = tuple(
        result
        for result in retrospective_history
        if (
            result.key == REAL_RATE_KEY
            and result.methodology_version == METHODOLOGY_VERSION
            and result.value is not None
            and result.as_of <= cutoff
            and (reference_date := parse_reference_date(result.reference_date)) is not None
            and reference_date <= cutoff.date()
            and bool(result.dependency_fingerprints)
            and all(item.available_at <= result.as_of for item in result.inputs)
        )
    )
    latest_by_reference: dict[date, DerivedResult] = {}
    for result in history:
        reference_date = parse_reference_date(result.reference_date)
        inputs = {item.key: item for item in result.inputs}
        if (
            result.key != REAL_RATE_KEY
            or result.methodology_version != METHODOLOGY_VERSION
            or result.value is None
            or reference_date is None
            or result.as_of > current.as_of
            or not {"nominal_rate", "expected_inflation"}.issubset(inputs)
            or any(item.available_at > result.as_of for item in inputs.values())
        ):
            continue
        latest_by_reference[reference_date] = result
    retrospective_references: set[date] = set()
    retrospective_available: dict[date, datetime] = {}
    for result in eligible_retrospective:
        reference_date = parse_reference_date(result.reference_date)
        if (
            result.key == REAL_RATE_KEY
            and result.methodology_version == METHODOLOGY_VERSION
            and result.value is not None
            and reference_date is not None
            and result.as_of <= current.as_of
            and result.dependency_fingerprints
            and all(item.available_at <= result.as_of for item in result.inputs)
            and reference_date not in latest_by_reference
        ):
            latest_by_reference[reference_date] = result
            retrospective_references.add(reference_date)
            retrospective_available[reference_date] = result.as_of
    current_reference = parse_reference_date(current.reference_date)
    if current_reference is not None:
        latest_by_reference[current_reference] = current
        retrospective_references.discard(current_reference)
    observations = tuple(
        PercentileObservation(
            value=result.value,
            as_of=datetime.combine(reference_date, time.min, timezone.utc),
        )
        for reference_date, result in sorted(latest_by_reference.items())
        if result.value is not None
    )
    if not retrospective_references:
        return observations, False, None
    latest_by_month: dict[tuple[int, int], tuple[date, PercentileObservation]] = {}
    for observation in observations:
        reference = observation.as_of.date()
        month_key = (reference.year, reference.month)
        previous = latest_by_month.get(month_key)
        if previous is None or reference > previous[0]:
            latest_by_month[month_key] = (reference, observation)
    selected = tuple(latest_by_month[key] for key in sorted(latest_by_month))
    if not any(reference in retrospective_references for reference, _point in selected):
        return observations, False, None
    selected_references = {
        reference for reference, _point in selected if reference in retrospective_references
    }
    latest_capture = max(retrospective_available[item] for item in selected_references)
    return tuple(point for _reference, point in selected), True, latest_capture


def _classify(
    result: DerivedResult,
    history: Sequence[DerivedResult],
    retrospective_history: Sequence[DerivedResult] = (),
) -> DerivedResult:
    if result.value is None:
        return result
    sample, uses_retrospective, history_available_at = _historical_observations(
        history, result, retrospective_history
    )
    observations = eligible_observations(sample, as_of=result.as_of)
    canonical_sample = tuple(
        (observation.as_of.date().isoformat(), observation.value)
        for observation in observations
    )
    basis = (
        _RETROSPECTIVE_HISTORY_BASIS
        if uses_retrospective
        else _PRODUCTION_HISTORY_BASIS
    )
    history_fingerprint = hashlib.sha256(
        json.dumps(
            {"basis": basis, "sample": canonical_sample},
            ensure_ascii=False,
            separators=(",", ":"),
            allow_nan=False,
        ).encode()
    ).hexdigest()
    percentile = empirical_percentile(
        observations,
        current_value=result.value,
        as_of=result.as_of,
    )
    return replace(
        result,
        historical_percentile=percentile.percentile,
        classification=(
            _CLASSIFICATIONS[percentile.band]
            if percentile.band is not None
            else None
        ),
        classification_reason=percentile.reason,
        classification_history_basis=basis,
        classification_history_count=len(observations),
        classification_history_available_at=history_available_at,
        dependency_fingerprints=(
            ("historical_sample", history_fingerprint),
            ("history_basis", hashlib.sha256(basis.encode()).hexdigest()),
        ),
    )


def build_real_rate_result(
    *,
    as_of: datetime,
    history: Sequence[DerivedResult],
    retrospective_history: Sequence[DerivedResult] = (),
) -> DerivedResult:
    cutoff = utc_datetime(as_of)
    focus_expectations = load_focus_expectations(as_of=cutoff)
    if not focus_expectations:
        return _unavailable(as_of=cutoff, reason=UnavailableReason.FOCUS_12M_MISSING)
    contracts = load_di_contracts(as_of=cutoff)
    if not contracts:
        return _unavailable(as_of=cutoff, reason=UnavailableReason.DI_CURVE_MISSING)

    reference_date = min(
        max(contract.trade_date for contract in contracts),
        max(expectation.reference_date for expectation in focus_expectations),
    )
    eligible_focus = tuple(
        expectation
        for expectation in focus_expectations
        if expectation.reference_date <= reference_date
    )
    if not eligible_focus:
        return _unavailable(
            as_of=cutoff,
            reason=UnavailableReason.FOCUS_12M_MISSING,
            reference_date=reference_date,
        )
    expectation = max(eligible_focus, key=lambda item: item.reference_date)

    eligible_curve_dates = tuple(
        contract.trade_date
        for contract in contracts
        if contract.trade_date <= reference_date
    )
    if not eligible_curve_dates:
        return _unavailable(
            as_of=cutoff,
            reason=UnavailableReason.DI_CURVE_MISSING,
            reference_date=reference_date,
            inputs=(_focus_input(expectation),),
        )
    curve_date = max(eligible_curve_dates)
    selected = select_di_contract(
        tuple(contract for contract in contracts if contract.trade_date == curve_date),
        reference_date=reference_date,
    )
    if selected is None:
        return _unavailable(
            as_of=cutoff,
            reason=UnavailableReason.DI_CONTRACT_OUTSIDE_TOLERANCE,
            reference_date=reference_date,
            inputs=(_focus_input(expectation),),
        )

    result = calculate(
        _SPEC,
        (_di_input(selected), _focus_input(expectation)),
        cutoff,
    )
    result = replace(result, reference_date=reference_date.isoformat())
    return _classify(result, history, retrospective_history)
