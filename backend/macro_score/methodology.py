from collections.abc import Mapping
from dataclasses import dataclass
from enum import StrEnum, unique
from types import MappingProxyType
from typing import Final, NewType

MacroComponentKey = NewType("MacroComponentKey", str)

MACRO_SCORE_VERSION: Final = "0.2.0-beta"


@unique
class Dimension(StrEnum):
    ACTIVITY = "activity"
    INFLATION = "inflation"
    MONETARY = "monetary"
    FISCAL = "fiscal"
    CREDIT = "credit"
    EXTERNAL = "external"


@unique
class Direction(StrEnum):
    HIGHER = "higher_is_better"
    LOWER = "lower_is_better"


@unique
class ComponentUnit(StrEnum):
    PCT = "pct"
    PCT_ANNUAL = "pct_annual"
    PCT_GDP = "pct_gdp"
    PP = "pp"
    RATIO = "ratio"
    USD_MILLION = "usd_million"


@dataclass(frozen=True, slots=True)
class ComponentSpec:
    key: MacroComponentKey
    dimension: Dimension
    direction: Direction
    unit: ComponentUnit
    formula: str
    source_keys: tuple[str, ...]
    weight: int = 1


COMPONENTS: Final[tuple[ComponentSpec, ...]] = (
    ComponentSpec(
        MacroComponentKey("ibc_br_sa_yoy"),
        Dimension.ACTIVITY,
        Direction.HIGHER,
        ComponentUnit.PCT,
        "((ibc_br_sa_t / ibc_br_sa_t-12) - 1) * 100",
        ("ibc_br_sa",),
    ),
    ComponentSpec(
        MacroComponentKey("desemprego"),
        Dimension.ACTIVITY,
        Direction.LOWER,
        ComponentUnit.PCT,
        "desemprego",
        ("desemprego",),
    ),
    ComponentSpec(
        MacroComponentKey("rendimento_real_yoy"),
        Dimension.ACTIVITY,
        Direction.HIGHER,
        ComponentUnit.PCT,
        "((rendimento_real_t / rendimento_real_t-12) - 1) * 100",
        ("rendimento_medio_real",),
    ),
    ComponentSpec(
        MacroComponentKey("abs_ipca_12m_target"),
        Dimension.INFLATION,
        Direction.LOWER,
        ComponentUnit.PP,
        "abs(ipca_12m - inflation_target)",
        ("ipca_12m", "inflation_target"),
    ),
    ComponentSpec(
        MacroComponentKey("abs_focus_12m_target"),
        Dimension.INFLATION,
        Direction.LOWER,
        ComponentUnit.PP,
        "abs(expected_inflation - inflation_target)",
        ("expected_inflation", "inflation_target"),
    ),
    ComponentSpec(
        MacroComponentKey("juro_real_ex_ante_1y"),
        Dimension.MONETARY,
        Direction.LOWER,
        ComponentUnit.PCT_ANNUAL,
        "((1 + DI/100) / (1 + IPCA/100) - 1) * 100",
        ("nominal_rate", "expected_inflation"),
    ),
    ComponentSpec(
        MacroComponentKey("inclinacao_di_5y_1y"),
        Dimension.MONETARY,
        Direction.HIGHER,
        ComponentUnit.PP,
        "di_5y - di_1y",
        ("di_5y", "di_1y"),
    ),
    ComponentSpec(
        MacroComponentKey("saldo_primario_12m_pib"),
        Dimension.FISCAL,
        Direction.HIGHER,
        ComponentUnit.PCT_GDP,
        "saldo_primario_12m_pib",
        ("saldo_primario_12m_pib",),
    ),
    ComponentSpec(
        MacroComponentKey("dbgg_pib"),
        Dimension.FISCAL,
        Direction.LOWER,
        ComponentUnit.PCT_GDP,
        "dbgg_pib",
        ("dbgg_pib",),
    ),
    ComponentSpec(
        MacroComponentKey("dbgg_yoy_pp"),
        Dimension.FISCAL,
        Direction.LOWER,
        ComponentUnit.PP,
        "dbgg_pib_t - dbgg_pib_t-12",
        ("dbgg_pib",),
    ),
    ComponentSpec(
        MacroComponentKey("saldo_nominal_12m_pib"),
        Dimension.FISCAL,
        Direction.HIGHER,
        ComponentUnit.PCT_GDP,
        "saldo_nominal_12m_pib",
        ("saldo_nominal_12m_pib",),
    ),
    ComponentSpec(
        MacroComponentKey("inadimplencia_pf"),
        Dimension.CREDIT,
        Direction.LOWER,
        ComponentUnit.PCT,
        "inadimplencia_pf",
        ("inadimplencia_pf",),
    ),
    ComponentSpec(
        MacroComponentKey("comprometimento_renda"),
        Dimension.CREDIT,
        Direction.LOWER,
        ComponentUnit.PCT,
        "comprometimento_renda",
        ("comprometimento_renda",),
    ),
    ComponentSpec(
        MacroComponentKey("icc_pf"),
        Dimension.CREDIT,
        Direction.LOWER,
        ComponentUnit.PCT_ANNUAL,
        "icc_pf",
        ("icc_pf",),
    ),
    ComponentSpec(
        MacroComponentKey("spread_credito_pf_selic"),
        Dimension.CREDIT,
        Direction.LOWER,
        ComponentUnit.PP,
        "taxa_media_credito_pf - selic_meta",
        ("taxa_media_credito_pf", "selic_meta"),
    ),
    ComponentSpec(
        MacroComponentKey("cobertura_externa"),
        Dimension.EXTERNAL,
        Direction.HIGHER,
        ComponentUnit.RATIO,
        "idp_12m_pib / abs(conta_corrente_12m_pib)",
        ("idp_12m_pib", "conta_corrente_12m_pib"),
    ),
    ComponentSpec(
        MacroComponentKey("conta_corrente_12m_pib"),
        Dimension.EXTERNAL,
        Direction.HIGHER,
        ComponentUnit.PCT_GDP,
        "conta_corrente_12m_pib",
        ("conta_corrente_12m_pib",),
    ),
    ComponentSpec(
        MacroComponentKey("reservas_internacionais"),
        Dimension.EXTERNAL,
        Direction.HIGHER,
        ComponentUnit.USD_MILLION,
        "reservas_internacionais",
        ("reservas_internacionais",),
    ),
)

COMPONENTS_BY_KEY: Final[Mapping[MacroComponentKey, ComponentSpec]] = MappingProxyType(
    {spec.key: spec for spec in COMPONENTS}
)
DIMENSION_MINIMUM_VALID: Final[Mapping[Dimension, int]] = MappingProxyType({
    Dimension.ACTIVITY: 2,
    Dimension.INFLATION: 2,
    Dimension.MONETARY: 2,
    Dimension.FISCAL: 3,
    Dimension.CREDIT: 3,
    Dimension.EXTERNAL: 2,
})
MINIMUM_VALID_DIMENSIONS: Final = 5
