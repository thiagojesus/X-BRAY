from collections.abc import Mapping
from decimal import Decimal
from enum import StrEnum
from types import MappingProxyType
from typing import Final

from domain_contracts import Record
from indicator_catalog import IndicatorKey, IndicatorSpec, Transformation


class SignConvention(StrEnum):
    DEBT_POSITIVE = "debt_positive"
    SURPLUS_POSITIVE = "surplus_positive"
    COST_POSITIVE = "cost_positive"


SIGN_CONVENTION: Final[Mapping[IndicatorKey, SignConvention]] = MappingProxyType(
    {
        IndicatorKey("dbgg_pib"): SignConvention.DEBT_POSITIVE,
        IndicatorKey("dlsp_pib"): SignConvention.DEBT_POSITIVE,
        IndicatorKey("saldo_primario_12m_pib"): SignConvention.SURPLUS_POSITIVE,
        IndicatorKey("juros_nominais_12m_pib"): SignConvention.COST_POSITIVE,
        IndicatorKey("saldo_nominal_12m_pib"): SignConvention.SURPLUS_POSITIVE,
    },
)
_SIGN_MULTIPLIER: Final[Mapping[Transformation | None, Decimal]] = MappingProxyType(
    {
        None: Decimal(1),
        Transformation.ANNUALIZE_12: Decimal(1),
        Transformation.ANNUALIZE_252: Decimal(1),
        Transformation.INVERT_SIGN: Decimal(-1),
    },
)


def present_record(record: Record, spec: IndicatorSpec) -> Record:
    value = Decimal(str(record["valor"])) * _SIGN_MULTIPLIER[spec.transformation]
    return {**record, "valor": format(value, ".2f")}
