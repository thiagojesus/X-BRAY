from collections.abc import Callable, Mapping
from dataclasses import dataclass
from datetime import datetime
from types import MappingProxyType
from typing import Final, Literal

from derived.credit_spread import RESULT_KEY as CREDIT_SPREAD_KEY
from derived.credit_spread import build_credit_spread_result
from derived.di_slope import DI_SLOPE_KEY, build_di_slope_result
from derived.external_coverage import (
    CURRENT_ACCOUNT_KEY,
    EXTERNAL_COVERAGE_KEY,
    IDP_KEY,
    build_external_coverage_result,
)
from derived.focus_target_distance import KEY as FOCUS_TARGET_DISTANCE_KEY
from derived.focus_target_distance import build_focus_target_distance_result
from derived.inflation_target_inputs import load_inflation_target_observations
from derived.models import DerivedResult, SnapshotOrigin
from derived.primary_balance_change import build_primary_balance_change_result
from derived.real_rate_inputs import load_di_contracts, load_focus_expectations
from derived.repository import insert_snapshot, refresh_derived_snapshots
from derived.sgs_inputs import load_sgs_observations
from indicator_catalog import IndicatorKey

AdditionalDerivedKey = Literal[
    "cobertura_externa",
    "variacao_saldo_primario_12m",
    "spread_credito_pf_selic",
    "distancia_focus_meta",
    "inclinacao_di_5y_1y",
]


@dataclass(frozen=True, slots=True)
class DerivedRefreshError(RuntimeError):
    inserted_count: int
    failed_keys: tuple[str, ...]

    def __str__(self) -> str:
        return f"derived snapshot failures: {', '.join(self.failed_keys)}"


def _external_coverage(as_of: datetime) -> DerivedResult:
    idp = load_sgs_observations(IDP_KEY, as_of=as_of)
    account = load_sgs_observations(CURRENT_ACCOUNT_KEY, as_of=as_of)
    return build_external_coverage_result(idp + account, as_of=as_of)


def _primary_balance_change(as_of: datetime) -> DerivedResult:
    observations = load_sgs_observations(
        IndicatorKey("saldo_primario_12m_pib"),
        as_of=as_of,
    )
    return build_primary_balance_change_result(observations, as_of=as_of)


def _credit_spread(as_of: datetime) -> DerivedResult:
    credit = load_sgs_observations(IndicatorKey("taxa_media_credito_pf"), as_of=as_of)
    selic = load_sgs_observations(IndicatorKey("selic_meta"), as_of=as_of)
    return build_credit_spread_result(credit + selic, as_of=as_of)


def _focus_target_distance(as_of: datetime) -> DerivedResult:
    return build_focus_target_distance_result(
        load_focus_expectations(as_of=as_of),
        target_observations=load_inflation_target_observations(as_of=as_of),
        as_of=as_of,
    )


def _di_slope(as_of: datetime) -> DerivedResult:
    return build_di_slope_result(load_di_contracts(as_of=as_of), as_of=as_of)


_BUILDERS: Final[Mapping[AdditionalDerivedKey, Callable[[datetime], DerivedResult]]] = MappingProxyType({
    EXTERNAL_COVERAGE_KEY: _external_coverage,
    "variacao_saldo_primario_12m": _primary_balance_change,
    CREDIT_SPREAD_KEY: _credit_spread,
    FOCUS_TARGET_DISTANCE_KEY: _focus_target_distance,
    DI_SLOPE_KEY: _di_slope,
})


def refresh_additional_snapshot(
    key: AdditionalDerivedKey,
    *,
    as_of: datetime,
    calculation_cycle_id: str,
) -> int:
    """Persist one additional indicator result as an immutable production snapshot."""
    result = _BUILDERS[key](as_of)
    return int(
        insert_snapshot(
            result,
            calculation_cycle_id=calculation_cycle_id,
            origin=SnapshotOrigin.PRODUCTION_REFRESH,
        )
    )


def refresh_all_derived_snapshots(as_of: datetime, calculation_cycle_id: str) -> int:
    operations: tuple[tuple[str, Callable[[], int]], ...] = (
        ("juro_real_ex_ante_1y", lambda: refresh_derived_snapshots(as_of, calculation_cycle_id)),
        *(
            (
                key,
                lambda key=key: refresh_additional_snapshot(
                    key,
                    as_of=as_of,
                    calculation_cycle_id=calculation_cycle_id,
                ),
            )
            for key in _BUILDERS
        ),
    )
    inserted_count = 0
    failures: list[str] = []
    for key, refresh in operations:
        try:
            inserted_count += refresh()
        except Exception:  # noqa: BLE001 - isolate each indicator's calculation and persistence failure.
            failures.append(key)
    if failures:
        raise DerivedRefreshError(inserted_count, tuple(failures))
    return inserted_count
