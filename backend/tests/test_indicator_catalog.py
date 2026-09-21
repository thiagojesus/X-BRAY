from dataclasses import FrozenInstanceError, replace
from importlib import import_module, util
from operator import setitem

import pytest

from config import (
    ACTIVITY,
    COMPLEMENTARY,
    CREDIT,
    EXCHANGE,
    FISCAL,
    INFLATION,
    INTEREST_RATES,
    IPCA_CORE,
    IPCA_GROUPS,
    IPCA_NATURE,
    IPCA_PRICES,
)
from indicator_catalog import (
    INDICATOR_CATALOG,
    CatalogGroup,
    DuplicateIndicatorError,
    Frequency,
    IndicatorKey,
    IndicatorSpec,
    InvalidIndicatorSpecError,
    Polarity,
    Provider,
    Transformation,
    Unit,
    build_catalog,
    default_freshness_days,
    legacy_codes,
)


LEGACY_ALIASES = {
    "IPCA_GROUPS": (
        IPCA_GROUPS,
        {
            "alimentacao_bebidas": 1635,
            "habitacao": 1636,
            "artigos_residencia": 1637,
            "vestuario": 1638,
            "transportes": 1639,
            "comunicacao": 1640,
            "saude_cuidados": 1641,
            "despesas_pessoais": 1642,
            "educacao": 1643,
        },
    ),
    "IPCA_NATURE": (
        IPCA_NATURE,
        {
            "bens_duraveis": 10843,
            "bens_semi_duraveis": 10842,
            "bens_nao_duraveis": 10841,
            "servicos": 10844,
        },
    ),
    "IPCA_CORE": (
        IPCA_CORE,
        {"core_medias_aparadas": 4466, "core_dp": 16122},
    ),
    "IPCA_PRICES": (
        IPCA_PRICES,
        {
            "itens_livres": 11428,
            "transacionaveis": 4447,
            "nao_transacionaveis": 4448,
            "administrados": 4449,
        },
    ),
    "INTEREST_RATES": (
        INTEREST_RATES,
        {"selic_meta": 432, "selic_efetiva": 11, "cdi": 12, "tr": 226},
    ),
    "INFLATION": (
        INFLATION,
        {
            "ipca": 433,
            "ipca_12m": 13522,
            "ipca_15": 7478,
            "inpc": 188,
            "igpm": 189,
            "igpdi": 190,
            "incc_di": 192,
        },
    ),
    "ACTIVITY": (
        ACTIVITY,
        {
            "pib": 1207,
            "ibc_br": 24363,
            "desemprego": 24369,
            "resultado_primario": 4639,
        },
    ),
    "FISCAL": (
        FISCAL,
        {
            "dbgg_pib": 13762,
            "dlsp_pib": 4513,
            "saldo_primario_12m_pib": 5793,
            "juros_nominais_12m_pib": 5760,
            "saldo_nominal_12m_pib": 5727,
        },
    ),
    "CREDIT": (
        CREDIT,
        {
            "inadimplencia_pf": 21084,
            "endividamento_familias": 29037,
            "comprometimento_renda": 29034,
            "icc_pf": 25353,
            "taxa_media_credito_pf": 20716,
        },
    ),
    "EXCHANGE": (
        EXCHANGE,
        {"ptax_compra_usd": 1, "ptax_venda_usd": 10813, "eur_brl": 21619},
    ),
    "COMPLEMENTARY": (
        COMPLEMENTARY,
        {
            "reservas_internacionais": 3546,
            "base_monetaria": 1835,
            "m0": 1788,
            "m1": 27788,
            "m2": 27810,
            "ic_commodities": 27574,
        },
    ),
}


def test_legacy_config_aliases_match_xbr_002_baseline() -> None:
    for actual, expected in LEGACY_ALIASES.values():
        assert type(actual) is dict
        assert actual == expected
        assert list(actual.items()) == list(expected.items())


def test_red_xbr_002_provides_an_immutable_unique_typed_catalog() -> None:
    catalog_module = util.find_spec("indicator_catalog")
    assert catalog_module is not None, "test_red_xbr_002 requires the typed catalog"

    module = import_module("indicator_catalog")
    catalog = module.INDICATOR_CATALOG
    specs = tuple(catalog.values())

    assert len(specs) == sum(len(actual) for actual, _expected in LEGACY_ALIASES.values())
    assert len({spec.key for spec in specs}) == len(specs)
    assert len({(spec.provider, spec.provider_code) for spec in specs}) == len(specs)
    assert all(spec.freshness_days > 0 and spec.source_url for spec in specs)

    with pytest.raises(FrozenInstanceError):
        specs[0].freshness_days = 0


def test_catalog_specs_have_valid_required_fields() -> None:
    for spec in INDICATOR_CATALOG.values():
        assert spec.key
        assert spec.provider_code > 0
        assert spec.label
        assert spec.short_label
        assert spec.start_date
        assert spec.freshness_days > 0
        assert spec.source_url.startswith("https://")


def test_catalog_default_freshness_days_are_valid_publication_delays() -> None:
    assert default_freshness_days(Frequency.DAILY) == 7
    assert default_freshness_days(Frequency.MARKET) == 7
    assert default_freshness_days(Frequency.MONTHLY) == 75
    assert default_freshness_days(Frequency.MOVING_QUARTER) == 120
    assert default_freshness_days(Frequency.QUARTERLY) == 120
    assert default_freshness_days(Frequency.ANNUAL) == 400


def test_catalog_aliases_are_valid_plain_dict_projections() -> None:
    for name, (actual, expected) in LEGACY_ALIASES.items():
        assert legacy_codes(CatalogGroup(name)) == expected
        assert actual == expected


def test_catalog_rejects_invalid_unit_and_frequency() -> None:
    with pytest.raises(ValueError):
        Unit("points")
    with pytest.raises(ValueError):
        Frequency("weekly")


@pytest.mark.parametrize(
    ("field", "value"),
    (
        ("provider", "not_a_provider"),
        ("unit", "not_a_unit"),
        ("frequency", "not_a_frequency"),
        ("polarity", "not_a_polarity"),
        ("transformation", "not_a_transformation"),
    ),
)
def test_indicator_spec_boundary_rejects_invalid_raw_closed_domains(
    field: str,
    value: str,
) -> None:
    valid = next(iter(INDICATOR_CATALOG.values()))

    with pytest.raises(InvalidIndicatorSpecError, match=field):
        replace(valid, **{field: value})


def test_indicator_spec_boundary_accepts_valid_enum_domains() -> None:
    spec = IndicatorSpec(
        key=IndicatorKey("valid_enum_domains"),
        group=CatalogGroup.ACTIVITY,
        provider=Provider.BCB_SGS,
        provider_code=999999,
        label="Valid domains",
        short_label="Valid",
        unit=Unit.INDEX,
        frequency=Frequency.MONTHLY,
        start_date="01/01/2020",
        freshness_days=75,
        polarity=Polarity.NEUTRAL,
        transformation=Transformation.ANNUALIZE_12,
        source_url="https://api.bcb.gov.br/dados/serie/999999",
    )

    assert spec.provider is Provider.BCB_SGS
    assert spec.unit is Unit.INDEX
    assert spec.frequency is Frequency.MONTHLY
    assert spec.polarity is Polarity.NEUTRAL
    assert spec.transformation is Transformation.ANNUALIZE_12


def test_catalog_rejects_duplicate_provider_codes() -> None:
    first = next(iter(INDICATOR_CATALOG.values()))
    duplicate = replace(first, key=IndicatorKey("duplicate_key"))

    with pytest.raises(DuplicateIndicatorError):
        build_catalog((first, duplicate))


def test_catalog_rejects_invalid_required_fields() -> None:
    first = next(iter(INDICATOR_CATALOG.values()))

    with pytest.raises(InvalidIndicatorSpecError):
        replace(first, freshness_days=0)
    with pytest.raises(InvalidIndicatorSpecError):
        replace(first, source_url="http://example.invalid")


def test_catalog_mapping_is_valid_and_immutable() -> None:
    first = next(iter(INDICATOR_CATALOG.values()))

    with pytest.raises(TypeError):
        setitem(INDICATOR_CATALOG, first.key, first)
