from collections.abc import Sequence
from datetime import date, datetime
from typing import Final

from derived.engine import DerivationSpec, calculate
from derived.models import DerivedInput, DerivedResult, UnavailableReason, utc_datetime
from derived.sgs_inputs import SGSObservation
from indicator_catalog import INDICATOR_CATALOG, IndicatorKey

EXTERNAL_COVERAGE_KEY: Final = "cobertura_externa"
METHODOLOGY_VERSION: Final = "1.0.0"
FORMULA: Final = "idp_12m_pib / abs(conta_corrente_12m_pib)"
IDP_KEY: Final = IndicatorKey("idp_12m_pib")
CURRENT_ACCOUNT_KEY: Final = IndicatorKey("conta_corrente_12m_pib")

_SPEC: Final = DerivationSpec(
    key=EXTERNAL_COVERAGE_KEY,
    unit="ratio",
    methodology_version=METHODOLOGY_VERSION,
    formula=FORMULA,
    calculate=lambda inputs: inputs[0].value / abs(inputs[1].value),
)


def _derived_input(observation: SGSObservation) -> DerivedInput:
    spec = INDICATOR_CATALOG[observation.indicator_key]
    return DerivedInput(
        key=str(observation.indicator_key),
        value=observation.value,
        unit=observation.unit.value,
        reference_period=observation.reference_period.isoformat(),
        available_at=observation.available_at,
        source=observation.source,
        metadata=(
            ("availability_basis", observation.availability_basis),
            ("catalog_frequency", observation.frequency.value),
            ("catalog_key", str(spec.key)),
            ("provider_code", int(observation.provider_code)),
        ),
    )


def _unavailable(
    *,
    as_of: datetime,
    reason: UnavailableReason,
    inputs: tuple[DerivedInput, ...] = (),
    reference_date: date | None = None,
) -> DerivedResult:
    return DerivedResult(
        key=EXTERNAL_COVERAGE_KEY,
        value=None,
        unit="ratio",
        as_of=as_of,
        methodology_version=METHODOLOGY_VERSION,
        formula=FORMULA,
        inputs=inputs,
        reason=reason,
        reference_date=reference_date.isoformat() if reference_date is not None else None,
    )


def build_external_coverage_result(
    observations: Sequence[SGSObservation],
    *,
    as_of: datetime,
) -> DerivedResult:
    """Build the IDP-to-current-account coverage ratio from eligible SGS observations."""
    cutoff = utc_datetime(as_of)
    latest_by_key: dict[IndicatorKey, SGSObservation] = {}
    for observation in observations:
        if (
            observation.indicator_key in (IDP_KEY, CURRENT_ACCOUNT_KEY)
            and observation.available_at <= cutoff
        ):
            current = latest_by_key.get(observation.indicator_key)
            if current is None or (
                observation.reference_period,
                observation.available_at,
                observation.first_seen_at,
            ) > (
                current.reference_period,
                current.available_at,
                current.first_seen_at,
            ):
                latest_by_key[observation.indicator_key] = observation

    idp = latest_by_key.get(IDP_KEY)
    account = latest_by_key.get(CURRENT_ACCOUNT_KEY)
    if idp is None or account is None:
        inputs = tuple(
            _derived_input(item)
            for item in (idp, account)
            if item is not None
        )
        return _unavailable(
            as_of=cutoff,
            reason=UnavailableReason.REQUIRED_INPUT_MISSING,
            inputs=inputs,
        )

    reference_date = min(idp.reference_period, account.reference_period)
    inputs = (_derived_input(idp), _derived_input(account))
    if (idp.reference_period.year, idp.reference_period.month) != (
        account.reference_period.year,
        account.reference_period.month,
    ):
        return _unavailable(
            as_of=cutoff,
            reason=UnavailableReason.REFERENCE_MONTH_MISMATCH,
            inputs=inputs,
            reference_date=reference_date,
        )
    if account.value >= 0:
        return _unavailable(
            as_of=cutoff,
            reason=UnavailableReason.CURRENT_ACCOUNT_NON_NEGATIVE,
            inputs=inputs,
            reference_date=reference_date,
        )

    result = calculate(_SPEC, inputs, cutoff)
    return DerivedResult(
        key=result.key,
        value=result.value,
        unit=result.unit,
        as_of=result.as_of,
        methodology_version=result.methodology_version,
        formula=result.formula,
        inputs=result.inputs,
        reason=result.reason,
        reference_date=reference_date.isoformat(),
    )
