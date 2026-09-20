from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from enum import StrEnum
from types import MappingProxyType
from typing import Final, NewType, assert_never


IndicatorKey = NewType("IndicatorKey", str)
ProviderCode = NewType("ProviderCode", int)


class CatalogGroup(StrEnum):
    IPCA_GROUPS = "IPCA_GROUPS"
    IPCA_NATURE = "IPCA_NATURE"
    IPCA_CORE = "IPCA_CORE"
    IPCA_PRICES = "IPCA_PRICES"
    INTEREST_RATES = "INTEREST_RATES"
    INFLATION = "INFLATION"
    ACTIVITY = "ACTIVITY"
    FISCAL = "FISCAL"
    EXCHANGE = "EXCHANGE"
    COMPLEMENTARY = "COMPLEMENTARY"


class Provider(StrEnum):
    BCB_SGS = "bcb_sgs"


class Unit(StrEnum):
    PCT = "pct"
    PCT_GDP = "pct_gdp"
    PCT_ANNUAL = "pct_annual"
    INDEX = "index"
    BRL = "brl"
    BRL_MILLION = "brl_million"
    USD_MILLION = "usd_million"
    RATIO = "ratio"


class Frequency(StrEnum):
    DAILY = "daily"
    MARKET = "market"
    MONTHLY = "monthly"
    MOVING_QUARTER = "moving_quarter"
    QUARTERLY = "quarterly"
    ANNUAL = "annual"


class Polarity(StrEnum):
    HIGHER_IS_BETTER = "higher_is_better"
    LOWER_IS_BETTER = "lower_is_better"
    NEUTRAL = "neutral"


class Transformation(StrEnum):
    ANNUALIZE_12 = "annualize_12"
    ANNUALIZE_252 = "annualize_252"
    INVERT_SIGN = "invert_sign"


@dataclass(frozen=True, slots=True)
class InvalidIndicatorSpecError(Exception):
    field: str
    value: str | int

    def __str__(self) -> str:
        return f"invalid indicator {self.field}: {self.value!r}"


@dataclass(frozen=True, slots=True)
class DuplicateIndicatorError(Exception):
    field: str
    value: str

    def __str__(self) -> str:
        return f"duplicate indicator {self.field}: {self.value}"


@dataclass(frozen=True, slots=True)
class IndicatorSpec:
    key: IndicatorKey
    group: CatalogGroup
    provider: Provider
    provider_code: ProviderCode
    label: str
    short_label: str
    unit: Unit
    frequency: Frequency
    start_date: str
    freshness_days: int
    polarity: Polarity
    transformation: Transformation | None
    source_url: str

    def __post_init__(self) -> None:
        required_strings = {
            "key": str(self.key),
            "label": self.label,
            "short_label": self.short_label,
            "start_date": self.start_date,
            "source_url": self.source_url,
        }
        for field, value in required_strings.items():
            if not value:
                raise InvalidIndicatorSpecError(field=field, value=value)
        if type(self.provider) is not Provider:
            raise InvalidIndicatorSpecError(field="provider", value=str(self.provider))
        if type(self.unit) is not Unit:
            raise InvalidIndicatorSpecError(field="unit", value=str(self.unit))
        if type(self.frequency) is not Frequency:
            raise InvalidIndicatorSpecError(field="frequency", value=str(self.frequency))
        if type(self.polarity) is not Polarity:
            raise InvalidIndicatorSpecError(field="polarity", value=str(self.polarity))
        if self.transformation is not None and type(self.transformation) is not Transformation:
            raise InvalidIndicatorSpecError(
                field="transformation",
                value=str(self.transformation),
            )
        if self.provider_code <= 0:
            raise InvalidIndicatorSpecError(field="provider_code", value=self.provider_code)
        if self.freshness_days <= 0:
            raise InvalidIndicatorSpecError(field="freshness_days", value=self.freshness_days)
        if not self.source_url.startswith("https://"):
            raise InvalidIndicatorSpecError(field="source_url", value=self.source_url)


def default_freshness_days(frequency: Frequency) -> int:
    match frequency:
        case Frequency.DAILY | Frequency.MARKET:
            return 7
        case Frequency.MONTHLY:
            return 75
        case Frequency.MOVING_QUARTER | Frequency.QUARTERLY:
            return 120
        case Frequency.ANNUAL:
            return 400
        case unreachable:
            assert_never(unreachable)


def build_catalog(specs: Sequence[IndicatorSpec]) -> Mapping[IndicatorKey, IndicatorSpec]:
    by_key: dict[IndicatorKey, IndicatorSpec] = {}
    provider_codes: set[tuple[Provider, ProviderCode]] = set()
    for spec in specs:
        if spec.key in by_key:
            raise DuplicateIndicatorError(field="key", value=spec.key)
        provider_code = (spec.provider, spec.provider_code)
        if provider_code in provider_codes:
            raise DuplicateIndicatorError(
                field="provider_code",
                value=f"{spec.provider.value}:{spec.provider_code}",
            )
        by_key[spec.key] = spec
        provider_codes.add(provider_code)
    return MappingProxyType(by_key)


def _source(code: ProviderCode) -> str:
    return f"https://api.bcb.gov.br/dados/serie/bcdata.sgs.{code}/dados"


_SPECS: Final[tuple[IndicatorSpec, ...]] = (
    IndicatorSpec(IndicatorKey("alimentacao_bebidas"), CatalogGroup.IPCA_GROUPS, Provider.BCB_SGS, ProviderCode(1635), "Alimentação e bebidas", "Alimentação", Unit.PCT, Frequency.MONTHLY, "01/01/1990", 75, Polarity.LOWER_IS_BETTER, None, _source(ProviderCode(1635))),
    IndicatorSpec(IndicatorKey("habitacao"), CatalogGroup.IPCA_GROUPS, Provider.BCB_SGS, ProviderCode(1636), "Habitação", "Habitação", Unit.PCT, Frequency.MONTHLY, "01/01/1990", 75, Polarity.LOWER_IS_BETTER, None, _source(ProviderCode(1636))),
    IndicatorSpec(IndicatorKey("artigos_residencia"), CatalogGroup.IPCA_GROUPS, Provider.BCB_SGS, ProviderCode(1637), "Artigos de residência", "Artigos residência", Unit.PCT, Frequency.MONTHLY, "01/01/1990", 75, Polarity.LOWER_IS_BETTER, None, _source(ProviderCode(1637))),
    IndicatorSpec(IndicatorKey("vestuario"), CatalogGroup.IPCA_GROUPS, Provider.BCB_SGS, ProviderCode(1638), "Vestuário", "Vestuário", Unit.PCT, Frequency.MONTHLY, "01/01/1990", 75, Polarity.LOWER_IS_BETTER, None, _source(ProviderCode(1638))),
    IndicatorSpec(IndicatorKey("transportes"), CatalogGroup.IPCA_GROUPS, Provider.BCB_SGS, ProviderCode(1639), "Transportes", "Transportes", Unit.PCT, Frequency.MONTHLY, "01/01/1990", 75, Polarity.LOWER_IS_BETTER, None, _source(ProviderCode(1639))),
    IndicatorSpec(IndicatorKey("comunicacao"), CatalogGroup.IPCA_GROUPS, Provider.BCB_SGS, ProviderCode(1640), "Comunicação", "Comunicação", Unit.PCT, Frequency.MONTHLY, "01/01/1990", 75, Polarity.LOWER_IS_BETTER, None, _source(ProviderCode(1640))),
    IndicatorSpec(IndicatorKey("saude_cuidados"), CatalogGroup.IPCA_GROUPS, Provider.BCB_SGS, ProviderCode(1641), "Saúde e cuidados pessoais", "Saúde", Unit.PCT, Frequency.MONTHLY, "01/01/1990", 75, Polarity.LOWER_IS_BETTER, None, _source(ProviderCode(1641))),
    IndicatorSpec(IndicatorKey("despesas_pessoais"), CatalogGroup.IPCA_GROUPS, Provider.BCB_SGS, ProviderCode(1642), "Despesas pessoais", "Despesas pessoais", Unit.PCT, Frequency.MONTHLY, "01/01/1990", 75, Polarity.LOWER_IS_BETTER, None, _source(ProviderCode(1642))),
    IndicatorSpec(IndicatorKey("educacao"), CatalogGroup.IPCA_GROUPS, Provider.BCB_SGS, ProviderCode(1643), "Educação", "Educação", Unit.PCT, Frequency.MONTHLY, "01/01/1990", 75, Polarity.LOWER_IS_BETTER, None, _source(ProviderCode(1643))),
    IndicatorSpec(IndicatorKey("bens_duraveis"), CatalogGroup.IPCA_NATURE, Provider.BCB_SGS, ProviderCode(10843), "Bens duráveis", "Duráveis", Unit.PCT, Frequency.MONTHLY, "01/01/1990", 75, Polarity.LOWER_IS_BETTER, None, _source(ProviderCode(10843))),
    IndicatorSpec(IndicatorKey("bens_semi_duraveis"), CatalogGroup.IPCA_NATURE, Provider.BCB_SGS, ProviderCode(10842), "Bens semiduráveis", "Semiduráveis", Unit.PCT, Frequency.MONTHLY, "01/01/1990", 75, Polarity.LOWER_IS_BETTER, None, _source(ProviderCode(10842))),
    IndicatorSpec(IndicatorKey("bens_nao_duraveis"), CatalogGroup.IPCA_NATURE, Provider.BCB_SGS, ProviderCode(10841), "Bens não duráveis", "Não duráveis", Unit.PCT, Frequency.MONTHLY, "01/01/1990", 75, Polarity.LOWER_IS_BETTER, None, _source(ProviderCode(10841))),
    IndicatorSpec(IndicatorKey("servicos"), CatalogGroup.IPCA_NATURE, Provider.BCB_SGS, ProviderCode(10844), "Serviços", "Serviços", Unit.PCT, Frequency.MONTHLY, "01/01/1990", 75, Polarity.LOWER_IS_BETTER, None, _source(ProviderCode(10844))),
    IndicatorSpec(IndicatorKey("core_medias_aparadas"), CatalogGroup.IPCA_CORE, Provider.BCB_SGS, ProviderCode(4466), "Núcleo por médias aparadas", "Médias aparadas", Unit.PCT, Frequency.MONTHLY, "01/01/1990", 75, Polarity.LOWER_IS_BETTER, None, _source(ProviderCode(4466))),
    IndicatorSpec(IndicatorKey("core_dp"), CatalogGroup.IPCA_CORE, Provider.BCB_SGS, ProviderCode(16122), "Núcleo de dupla ponderação", "Dupla ponderação", Unit.PCT, Frequency.MONTHLY, "01/01/1990", 75, Polarity.LOWER_IS_BETTER, None, _source(ProviderCode(16122))),
    IndicatorSpec(IndicatorKey("itens_livres"), CatalogGroup.IPCA_PRICES, Provider.BCB_SGS, ProviderCode(11428), "Preços livres", "Livres", Unit.PCT, Frequency.MONTHLY, "01/01/1990", 75, Polarity.LOWER_IS_BETTER, None, _source(ProviderCode(11428))),
    IndicatorSpec(IndicatorKey("transacionaveis"), CatalogGroup.IPCA_PRICES, Provider.BCB_SGS, ProviderCode(4447), "Bens comercializáveis", "Comercializáveis", Unit.PCT, Frequency.MONTHLY, "01/01/1990", 75, Polarity.LOWER_IS_BETTER, None, _source(ProviderCode(4447))),
    IndicatorSpec(IndicatorKey("nao_transacionaveis"), CatalogGroup.IPCA_PRICES, Provider.BCB_SGS, ProviderCode(4448), "Bens não comercializáveis", "Não comercializáveis", Unit.PCT, Frequency.MONTHLY, "01/01/1990", 75, Polarity.LOWER_IS_BETTER, None, _source(ProviderCode(4448))),
    IndicatorSpec(IndicatorKey("administrados"), CatalogGroup.IPCA_PRICES, Provider.BCB_SGS, ProviderCode(4449), "Preços monitorados", "Monitorados", Unit.PCT, Frequency.MONTHLY, "01/01/1990", 75, Polarity.LOWER_IS_BETTER, None, _source(ProviderCode(4449))),
    IndicatorSpec(IndicatorKey("selic_meta"), CatalogGroup.INTEREST_RATES, Provider.BCB_SGS, ProviderCode(432), "Taxa Selic - meta", "Selic Meta", Unit.PCT_ANNUAL, Frequency.DAILY, "01/01/2015", 7, Polarity.NEUTRAL, None, _source(ProviderCode(432))),
    IndicatorSpec(IndicatorKey("selic_efetiva"), CatalogGroup.INTEREST_RATES, Provider.BCB_SGS, ProviderCode(11), "Taxa Selic efetiva", "Selic Efetiva", Unit.PCT_ANNUAL, Frequency.DAILY, "01/01/2015", 7, Polarity.NEUTRAL, Transformation.ANNUALIZE_252, _source(ProviderCode(11))),
    IndicatorSpec(IndicatorKey("cdi"), CatalogGroup.INTEREST_RATES, Provider.BCB_SGS, ProviderCode(12), "Taxa CDI", "CDI", Unit.PCT_ANNUAL, Frequency.DAILY, "01/01/2015", 7, Polarity.NEUTRAL, Transformation.ANNUALIZE_252, _source(ProviderCode(12))),
    IndicatorSpec(IndicatorKey("tr"), CatalogGroup.INTEREST_RATES, Provider.BCB_SGS, ProviderCode(226), "Taxa Referencial", "TR", Unit.PCT_ANNUAL, Frequency.MONTHLY, "01/01/1991", 75, Polarity.NEUTRAL, Transformation.ANNUALIZE_12, _source(ProviderCode(226))),
    IndicatorSpec(IndicatorKey("ipca"), CatalogGroup.INFLATION, Provider.BCB_SGS, ProviderCode(433), "IPCA mensal", "IPCA", Unit.PCT, Frequency.MONTHLY, "01/01/2015", 75, Polarity.LOWER_IS_BETTER, None, _source(ProviderCode(433))),
    IndicatorSpec(IndicatorKey("ipca_12m"), CatalogGroup.INFLATION, Provider.BCB_SGS, ProviderCode(13522), "IPCA acumulado em 12 meses", "IPCA 12m", Unit.PCT, Frequency.MONTHLY, "01/01/2015", 75, Polarity.LOWER_IS_BETTER, None, _source(ProviderCode(13522))),
    IndicatorSpec(IndicatorKey("ipca_15"), CatalogGroup.INFLATION, Provider.BCB_SGS, ProviderCode(7478), "IPCA-15 mensal", "IPCA-15", Unit.PCT, Frequency.MONTHLY, "01/01/2015", 75, Polarity.LOWER_IS_BETTER, None, _source(ProviderCode(7478))),
    IndicatorSpec(IndicatorKey("inpc"), CatalogGroup.INFLATION, Provider.BCB_SGS, ProviderCode(188), "INPC mensal", "INPC", Unit.PCT, Frequency.MONTHLY, "01/01/2015", 75, Polarity.LOWER_IS_BETTER, None, _source(ProviderCode(188))),
    IndicatorSpec(IndicatorKey("igpm"), CatalogGroup.INFLATION, Provider.BCB_SGS, ProviderCode(189), "IGP-M mensal", "IGP-M", Unit.PCT, Frequency.MONTHLY, "01/01/2015", 75, Polarity.LOWER_IS_BETTER, None, _source(ProviderCode(189))),
    IndicatorSpec(IndicatorKey("igpdi"), CatalogGroup.INFLATION, Provider.BCB_SGS, ProviderCode(190), "IGP-DI mensal", "IGP-DI", Unit.PCT, Frequency.MONTHLY, "01/01/2015", 75, Polarity.LOWER_IS_BETTER, None, _source(ProviderCode(190))),
    IndicatorSpec(IndicatorKey("incc_di"), CatalogGroup.INFLATION, Provider.BCB_SGS, ProviderCode(192), "INCC-DI mensal", "INCC-DI", Unit.PCT, Frequency.MONTHLY, "01/01/2015", 75, Polarity.LOWER_IS_BETTER, None, _source(ProviderCode(192))),
    IndicatorSpec(IndicatorKey("pib"), CatalogGroup.ACTIVITY, Provider.BCB_SGS, ProviderCode(1207), "Produto Interno Bruto", "PIB", Unit.BRL_MILLION, Frequency.QUARTERLY, "01/01/2015", 120, Polarity.HIGHER_IS_BETTER, None, _source(ProviderCode(1207))),
    IndicatorSpec(IndicatorKey("ibc_br"), CatalogGroup.ACTIVITY, Provider.BCB_SGS, ProviderCode(24363), "Índice de Atividade Econômica do Banco Central", "IBC-Br", Unit.INDEX, Frequency.MONTHLY, "01/01/2015", 75, Polarity.HIGHER_IS_BETTER, None, _source(ProviderCode(24363))),
    IndicatorSpec(IndicatorKey("desemprego"), CatalogGroup.ACTIVITY, Provider.BCB_SGS, ProviderCode(24369), "Taxa de desocupação", "Desemprego", Unit.PCT, Frequency.MOVING_QUARTER, "01/01/2015", 120, Polarity.LOWER_IS_BETTER, None, _source(ProviderCode(24369))),
    IndicatorSpec(IndicatorKey("resultado_primario"), CatalogGroup.ACTIVITY, Provider.BCB_SGS, ProviderCode(4639), "Resultado primário do setor público", "Resultado primário", Unit.BRL_MILLION, Frequency.MONTHLY, "01/01/2015", 75, Polarity.HIGHER_IS_BETTER, None, _source(ProviderCode(4639))),
    IndicatorSpec(IndicatorKey("dbgg_pib"), CatalogGroup.FISCAL, Provider.BCB_SGS, ProviderCode(13762), "Dívida bruta do governo geral", "DBGG", Unit.PCT_GDP, Frequency.MONTHLY, "01/12/2006", 75, Polarity.LOWER_IS_BETTER, None, _source(ProviderCode(13762))),
    IndicatorSpec(IndicatorKey("dlsp_pib"), CatalogGroup.FISCAL, Provider.BCB_SGS, ProviderCode(4513), "Dívida líquida do setor público", "DLSP", Unit.PCT_GDP, Frequency.MONTHLY, "01/01/2001", 75, Polarity.LOWER_IS_BETTER, None, _source(ProviderCode(4513))),
    IndicatorSpec(IndicatorKey("saldo_primario_12m_pib"), CatalogGroup.FISCAL, Provider.BCB_SGS, ProviderCode(5793), "Resultado primário acumulado em 12 meses", "Primário 12m", Unit.PCT_GDP, Frequency.MONTHLY, "01/12/2001", 75, Polarity.HIGHER_IS_BETTER, Transformation.INVERT_SIGN, _source(ProviderCode(5793))),
    IndicatorSpec(IndicatorKey("juros_nominais_12m_pib"), CatalogGroup.FISCAL, Provider.BCB_SGS, ProviderCode(5760), "Juros nominais acumulados em 12 meses", "Juros nominais 12m", Unit.PCT_GDP, Frequency.MONTHLY, "01/12/2001", 75, Polarity.LOWER_IS_BETTER, None, _source(ProviderCode(5760))),
    IndicatorSpec(IndicatorKey("saldo_nominal_12m_pib"), CatalogGroup.FISCAL, Provider.BCB_SGS, ProviderCode(5727), "Resultado nominal acumulado em 12 meses", "Nominal 12m", Unit.PCT_GDP, Frequency.MONTHLY, "01/12/2001", 75, Polarity.HIGHER_IS_BETTER, Transformation.INVERT_SIGN, _source(ProviderCode(5727))),
    IndicatorSpec(IndicatorKey("ptax_compra_usd"), CatalogGroup.EXCHANGE, Provider.BCB_SGS, ProviderCode(1), "Taxa de câmbio - compra", "PTAX compra", Unit.RATIO, Frequency.MARKET, "01/01/2015", 7, Polarity.NEUTRAL, None, _source(ProviderCode(1))),
    IndicatorSpec(IndicatorKey("ptax_venda_usd"), CatalogGroup.EXCHANGE, Provider.BCB_SGS, ProviderCode(10813), "Taxa de câmbio - venda", "PTAX venda", Unit.RATIO, Frequency.MARKET, "01/01/2015", 7, Polarity.NEUTRAL, None, _source(ProviderCode(10813))),
    IndicatorSpec(IndicatorKey("eur_brl"), CatalogGroup.EXCHANGE, Provider.BCB_SGS, ProviderCode(21619), "Taxa de câmbio EUR/BRL", "EUR/BRL", Unit.RATIO, Frequency.MARKET, "01/01/2015", 7, Polarity.NEUTRAL, None, _source(ProviderCode(21619))),
    IndicatorSpec(IndicatorKey("reservas_internacionais"), CatalogGroup.COMPLEMENTARY, Provider.BCB_SGS, ProviderCode(3546), "Reservas internacionais", "Reservas", Unit.USD_MILLION, Frequency.DAILY, "01/01/2015", 7, Polarity.HIGHER_IS_BETTER, None, _source(ProviderCode(3546))),
    IndicatorSpec(IndicatorKey("base_monetaria"), CatalogGroup.COMPLEMENTARY, Provider.BCB_SGS, ProviderCode(1835), "Base monetária", "Base monetária", Unit.BRL_MILLION, Frequency.MONTHLY, "01/01/2015", 75, Polarity.NEUTRAL, None, _source(ProviderCode(1835))),
    IndicatorSpec(IndicatorKey("m0"), CatalogGroup.COMPLEMENTARY, Provider.BCB_SGS, ProviderCode(1788), "Papel-moeda emitido", "M0", Unit.BRL_MILLION, Frequency.MONTHLY, "01/01/2015", 75, Polarity.NEUTRAL, None, _source(ProviderCode(1788))),
    IndicatorSpec(IndicatorKey("m1"), CatalogGroup.COMPLEMENTARY, Provider.BCB_SGS, ProviderCode(27788), "Meios de pagamento restritos", "M1", Unit.BRL_MILLION, Frequency.MONTHLY, "01/01/2015", 75, Polarity.NEUTRAL, None, _source(ProviderCode(27788))),
    IndicatorSpec(IndicatorKey("m2"), CatalogGroup.COMPLEMENTARY, Provider.BCB_SGS, ProviderCode(27810), "Meios de pagamento amplos", "M2", Unit.BRL_MILLION, Frequency.MONTHLY, "01/01/2015", 75, Polarity.NEUTRAL, None, _source(ProviderCode(27810))),
    IndicatorSpec(IndicatorKey("ic_commodities"), CatalogGroup.COMPLEMENTARY, Provider.BCB_SGS, ProviderCode(27574), "Índice de Commodities Brasil", "IC-Br", Unit.INDEX, Frequency.MONTHLY, "01/01/2015", 75, Polarity.NEUTRAL, None, _source(ProviderCode(27574))),
)


INDICATOR_CATALOG: Final[Mapping[IndicatorKey, IndicatorSpec]] = build_catalog(_SPECS)


def legacy_codes(group: CatalogGroup) -> dict[str, int]:
    return {
        str(spec.key): int(spec.provider_code)
        for spec in INDICATOR_CATALOG.values()
        if spec.group is group
    }
