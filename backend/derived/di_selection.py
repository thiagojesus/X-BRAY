from collections.abc import Sequence
from datetime import date, timedelta
from typing import Final

from derived.real_rate_inputs import DIContract

CONTRACT_TOLERANCE_DAYS: Final = 90


def select_di_contract_for_horizon(
    contracts: Sequence[DIContract],
    reference_date: date,
    horizon_days: int,
) -> DIContract | None:
    target = reference_date + timedelta(days=horizon_days)
    candidates = tuple(
        contract
        for contract in contracts
        if abs((contract.maturity - target).days) <= CONTRACT_TOLERANCE_DAYS
    )
    if not candidates:
        return None
    return min(
        candidates,
        key=lambda contract: (
            abs((contract.maturity - target).days),
            contract.maturity,
            contract.symbol,
        ),
    )
