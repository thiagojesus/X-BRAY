from collections.abc import Sequence
from datetime import date, datetime
from typing import Final

from derived.di_selection import select_di_contract_for_horizon
from derived.engine import DerivationSpec
from derived.models import DerivedInput, DerivedResult, UnavailableReason, utc_datetime
from derived.real_rate_inputs import DIContract

DI_SLOPE_KEY: Final = "inclinacao_di_5y_1y"
METHODOLOGY_VERSION: Final = "1.0.0"
FORMULA: Final = "DI5Y - DI1Y"


def _calculate_slope(inputs: tuple[DerivedInput, ...]) -> float:
    values = {item.key: item.value for item in inputs}
    return values["di_5y"] - values["di_1y"]


SPEC: Final = DerivationSpec(
    key=DI_SLOPE_KEY,
    unit="pp",
    methodology_version=METHODOLOGY_VERSION,
    formula=FORMULA,
    calculate=_calculate_slope,
)


def _di_input(key: str, contract: DIContract) -> DerivedInput:
    return DerivedInput(
        key=key,
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


def _unavailable(
    *,
    as_of: datetime,
    reason: UnavailableReason,
    reference_date: date | None = None,
    inputs: tuple[DerivedInput, ...] = (),
) -> DerivedResult:
    return DerivedResult(
        key=DI_SLOPE_KEY,
        value=None,
        unit="pp",
        as_of=as_of,
        methodology_version=METHODOLOGY_VERSION,
        formula=FORMULA,
        inputs=inputs,
        reason=reason,
        reference_date=reference_date.isoformat() if reference_date is not None else None,
    )


def build_di_slope_result(
    contracts: Sequence[DIContract],
    *,
    as_of: datetime,
) -> DerivedResult:
    cutoff = utc_datetime(as_of)
    eligible = tuple(contract for contract in contracts if contract.available_at <= cutoff)
    if not eligible:
        return _unavailable(as_of=cutoff, reason=UnavailableReason.DI_CURVE_MISSING)

    trade_date = max(contract.trade_date for contract in eligible)
    curve = tuple(contract for contract in eligible if contract.trade_date == trade_date)
    one_year = select_di_contract_for_horizon(curve, trade_date, 365)
    five_year = select_di_contract_for_horizon(curve, trade_date, 1825)
    if one_year is None or five_year is None:
        return _unavailable(
            as_of=cutoff,
            reason=UnavailableReason.DI_CONTRACT_OUTSIDE_TOLERANCE,
            reference_date=trade_date,
        )
    if (one_year.symbol, one_year.maturity) == (five_year.symbol, five_year.maturity):
        return _unavailable(
            as_of=cutoff,
            reason=UnavailableReason.DI_CONTRACT_REUSED,
            reference_date=trade_date,
        )

    inputs = (_di_input("di_1y", one_year), _di_input("di_5y", five_year))
    return DerivedResult(
        key=DI_SLOPE_KEY,
        value=_calculate_slope(inputs),
        unit="pp",
        as_of=cutoff,
        methodology_version=METHODOLOGY_VERSION,
        formula=FORMULA,
        inputs=inputs,
        reason=None,
        reference_date=trade_date.isoformat(),
    )
