from collections.abc import Sequence
from datetime import date, datetime
from typing import Final

from datafetchers.focus import FOCUS_12M_FILTER
from derived.engine import DerivationSpec, calculate
from derived.inflation_target_inputs import (
    InflationTargetLedgerError,
    InflationTargetObservation,
)
from derived.inflation_targets import target_for_reference_date
from derived.models import DerivedInput, DerivedResult, UnavailableReason, utc_datetime
from derived.real_rate_inputs import FocusExpectation

KEY: Final = "distancia_focus_meta"
METHODOLOGY_VERSION: Final = "1.0.0"
FORMULA: Final = "Focus IPCA 12m - centro da meta de inflação"
_TARGET_TABLE_SOURCE: Final = "https://www.bcb.gov.br/controleinflacao/historicometas"


def _difference(inputs: tuple[DerivedInput, ...]) -> float:
    values = {item.key: item.value for item in inputs}
    return values["expected_inflation"] - values["inflation_target"]


SPEC: Final = DerivationSpec(
    key=KEY,
    unit="pp",
    methodology_version=METHODOLOGY_VERSION,
    formula=FORMULA,
    calculate=_difference,
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


def _target_metadata(
    observation: InflationTargetObservation,
) -> tuple[tuple[str, str | int | float | None], ...]:
    rule = observation.rule
    return (
        ("availability_basis", "first_seen_at"),
        ("effective_from", rule.effective_from.isoformat()),
        ("effective_to", rule.effective_to.isoformat() if rule.effective_to else None),
        ("first_seen_at", observation.first_seen_at.isoformat()),
        ("legal_reference", rule.legal_reference),
        ("payload_hash", observation.payload_hash),
        ("regime", rule.regime),
        ("rule_id", rule.rule_id),
        ("source_url", rule.direct_source_url or rule.source_url or _TARGET_TABLE_SOURCE),
        ("table_version", rule.table_version),
        ("tolerance_band_pp", rule.tolerance_band_pp),
    )


def _target_input(
    observation: InflationTargetObservation,
    reference_date: date,
) -> DerivedInput:
    rule = observation.rule
    return DerivedInput(
        key="inflation_target",
        value=rule.center_pct,
        unit="pct_annual",
        reference_period=reference_date.isoformat(),
        available_at=observation.available_at,
        source=rule.direct_source_url or rule.source_url or _TARGET_TABLE_SOURCE,
        metadata=_target_metadata(observation),
    )


def _unavailable(
    as_of: datetime,
    reason: UnavailableReason,
    reference_date: date | None = None,
    inputs: tuple[DerivedInput, ...] = (),
) -> DerivedResult:
    return DerivedResult(
        key=SPEC.key,
        value=None,
        unit=SPEC.unit,
        as_of=as_of,
        methodology_version=SPEC.methodology_version,
        formula=SPEC.formula,
        inputs=inputs,
        reason=reason,
        reference_date=reference_date.isoformat() if reference_date else None,
    )


def _latest_target_revisions(
    observations: Sequence[InflationTargetObservation],
    *,
    as_of: datetime,
) -> tuple[InflationTargetObservation, ...]:
    revisions: dict[tuple[str, str, str], InflationTargetObservation] = {}
    for observation in observations:
        first_seen_at = utc_datetime(observation.first_seen_at)
        if first_seen_at > as_of:
            continue
        rule = observation.rule
        revision_key = (rule.rule_id, rule.table_version, observation.payload_hash)
        existing = revisions.get(revision_key)
        if existing is None or first_seen_at < existing.first_seen_at:
            revisions[revision_key] = observation

    latest_by_rule: dict[str, InflationTargetObservation] = {}
    for observation in revisions.values():
        rule_id = observation.rule.rule_id
        existing = latest_by_rule.get(rule_id)
        if existing is None or observation.first_seen_at > existing.first_seen_at:
            latest_by_rule[rule_id] = observation
        elif (
            observation.first_seen_at == existing.first_seen_at
            and (observation.rule.table_version, observation.payload_hash)
            != (existing.rule.table_version, existing.payload_hash)
        ):
            raise InflationTargetLedgerError("ambiguous_revision")
    return tuple(latest_by_rule.values())


def _select_target_observation(
    observations: Sequence[InflationTargetObservation],
    *,
    reference_date: date,
    as_of: datetime,
) -> InflationTargetObservation | None:
    latest = _latest_target_revisions(observations, as_of=as_of)
    applicable = tuple(
        observation
        for observation in latest
        if observation.rule.effective_from <= reference_date
        and (
            observation.rule.effective_to is None
            or reference_date < observation.rule.effective_to
        )
    )
    if len(applicable) > 1:
        raise InflationTargetLedgerError("ambiguous_rule")
    return applicable[0] if applicable else None


def build_focus_target_distance_result(
    expectations: Sequence[FocusExpectation],
    *,
    target_observations: Sequence[InflationTargetObservation],
    as_of: datetime,
) -> DerivedResult:
    cutoff = utc_datetime(as_of)
    eligible = tuple(
        expectation
        for expectation in expectations
        if expectation.available_at <= cutoff
    )
    if not eligible:
        return _unavailable(cutoff, UnavailableReason.FOCUS_12M_MISSING)

    expectation = max(eligible, key=lambda item: (item.reference_date, item.available_at))
    target_observation = _select_target_observation(
        target_observations,
        reference_date=expectation.reference_date,
        as_of=cutoff,
    )
    if target_observation is None:
        current_rule = target_for_reference_date(expectation.reference_date)
        if current_rule is None or any(
            observation.rule.rule_id == current_rule.rule_id
            and observation.first_seen_at <= cutoff
            for observation in target_observations
        ):
            reason = UnavailableReason.INFLATION_TARGET_MISSING
        else:
            reason = UnavailableReason.INPUT_NOT_AVAILABLE
        return _unavailable(
            cutoff,
            reason,
            expectation.reference_date,
            (_focus_input(expectation),),
        )
    inputs = (
        _focus_input(expectation),
        _target_input(target_observation, expectation.reference_date),
    )

    result = calculate(SPEC, inputs, cutoff)
    return DerivedResult(
        key=result.key,
        value=result.value,
        unit=result.unit,
        as_of=result.as_of,
        methodology_version=result.methodology_version,
        formula=result.formula,
        inputs=result.inputs,
        reason=result.reason,
        reference_date=expectation.reference_date.isoformat(),
    )
