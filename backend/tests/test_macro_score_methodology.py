from pathlib import Path

import pytest


def _require_methodology_module() -> None:
    methodology_path = Path(__file__).parents[1] / "macro_score" / "methodology.py"
    if not methodology_path.is_file():
        pytest.fail(
            "[RED XBR-601] the Macro Score methodology module has not been implemented"
        )


def test_red_xbr_601_methodology_freezes_all_component_contracts() -> None:
    _require_methodology_module()
    from macro_score.methodology import (
        COMPONENTS,
        MACRO_SCORE_VERSION,
        ComponentUnit,
        Dimension,
        Direction,
    )

    components = {str(spec.key): spec for spec in COMPONENTS}
    expected: dict[
        str,
        tuple[Dimension, Direction, ComponentUnit, str, tuple[str, ...]],
    ] = {
        "ibc_br_sa_yoy": (
            Dimension.ACTIVITY,
            Direction.HIGHER,
            ComponentUnit.PCT,
            "((ibc_br_sa_t / ibc_br_sa_t-12) - 1) * 100",
            ("ibc_br_sa",),
        ),
        "desemprego": (
            Dimension.ACTIVITY,
            Direction.LOWER,
            ComponentUnit.PCT,
            "desemprego",
            ("desemprego",),
        ),
        "rendimento_real_yoy": (
            Dimension.ACTIVITY,
            Direction.HIGHER,
            ComponentUnit.PCT,
            "((rendimento_real_t / rendimento_real_t-12) - 1) * 100",
            ("rendimento_medio_real",),
        ),
        "abs_ipca_12m_target": (
            Dimension.INFLATION,
            Direction.LOWER,
            ComponentUnit.PP,
            "abs(ipca_12m - inflation_target)",
            ("ipca_12m", "inflation_target"),
        ),
        "abs_focus_12m_target": (
            Dimension.INFLATION,
            Direction.LOWER,
            ComponentUnit.PP,
            "abs(expected_inflation - inflation_target)",
            ("expected_inflation", "inflation_target"),
        ),
        "juro_real_ex_ante_1y": (
            Dimension.MONETARY,
            Direction.LOWER,
            ComponentUnit.PCT_ANNUAL,
            "((1 + DI/100) / (1 + IPCA/100) - 1) * 100",
            ("nominal_rate", "expected_inflation"),
        ),
        "inclinacao_di_5y_1y": (
            Dimension.MONETARY,
            Direction.HIGHER,
            ComponentUnit.PP,
            "di_5y - di_1y",
            ("di_5y", "di_1y"),
        ),
        "saldo_primario_12m_pib": (
            Dimension.FISCAL,
            Direction.HIGHER,
            ComponentUnit.PCT_GDP,
            "saldo_primario_12m_pib",
            ("saldo_primario_12m_pib",),
        ),
        "dbgg_pib": (
            Dimension.FISCAL,
            Direction.LOWER,
            ComponentUnit.PCT_GDP,
            "dbgg_pib",
            ("dbgg_pib",),
        ),
        "dbgg_yoy_pp": (
            Dimension.FISCAL,
            Direction.LOWER,
            ComponentUnit.PP,
            "dbgg_pib_t - dbgg_pib_t-12",
            ("dbgg_pib",),
        ),
        "saldo_nominal_12m_pib": (
            Dimension.FISCAL,
            Direction.HIGHER,
            ComponentUnit.PCT_GDP,
            "saldo_nominal_12m_pib",
            ("saldo_nominal_12m_pib",),
        ),
        "inadimplencia_pf": (
            Dimension.CREDIT,
            Direction.LOWER,
            ComponentUnit.PCT,
            "inadimplencia_pf",
            ("inadimplencia_pf",),
        ),
        "comprometimento_renda": (
            Dimension.CREDIT,
            Direction.LOWER,
            ComponentUnit.PCT,
            "comprometimento_renda",
            ("comprometimento_renda",),
        ),
        "icc_pf": (
            Dimension.CREDIT,
            Direction.LOWER,
            ComponentUnit.PCT_ANNUAL,
            "icc_pf",
            ("icc_pf",),
        ),
        "spread_credito_pf_selic": (
            Dimension.CREDIT,
            Direction.LOWER,
            ComponentUnit.PP,
            "taxa_media_credito_pf - selic_meta",
            ("taxa_media_credito_pf", "selic_meta"),
        ),
        "cobertura_externa": (
            Dimension.EXTERNAL,
            Direction.HIGHER,
            ComponentUnit.RATIO,
            "idp_12m_pib / abs(conta_corrente_12m_pib)",
            ("idp_12m_pib", "conta_corrente_12m_pib"),
        ),
        "conta_corrente_12m_pib": (
            Dimension.EXTERNAL,
            Direction.HIGHER,
            ComponentUnit.PCT_GDP,
            "conta_corrente_12m_pib",
            ("conta_corrente_12m_pib",),
        ),
        "reservas_internacionais": (
            Dimension.EXTERNAL,
            Direction.HIGHER,
            ComponentUnit.USD_MILLION,
            "reservas_internacionais",
            ("reservas_internacionais",),
        ),
    }

    assert MACRO_SCORE_VERSION == "0.3.0-beta"
    assert set(components) == set(expected)
    assert len(COMPONENTS) == 18
    assert all(
        (
            components[key].dimension,
            components[key].direction,
            components[key].unit,
            components[key].formula,
            components[key].source_keys,
        )
        == contract
        for key, contract in expected.items()
    )
    assert all(spec.weight == 1 for spec in COMPONENTS)


def test_macro_score_methodology_keeps_six_dimensions_with_expected_component_counts() -> (
    None
):
    _require_methodology_module()
    from macro_score.methodology import COMPONENTS, Dimension

    counts = {
        dimension: sum(spec.dimension is dimension for spec in COMPONENTS)
        for dimension in Dimension
    }

    assert counts == {
        Dimension.ACTIVITY: 3,
        Dimension.INFLATION: 2,
        Dimension.MONETARY: 2,
        Dimension.FISCAL: 4,
        Dimension.CREDIT: 4,
        Dimension.EXTERNAL: 3,
    }


def test_macro_score_coverage_thresholds_are_not_rounded() -> None:
    _require_methodology_module()
    from macro_score.methodology import (
        DIMENSION_MINIMUM_VALID,
        MINIMUM_VALID_DIMENSIONS,
        Dimension,
    )

    assert DIMENSION_MINIMUM_VALID == {
        Dimension.ACTIVITY: 2,
        Dimension.INFLATION: 2,
        Dimension.MONETARY: 2,
        Dimension.FISCAL: 3,
        Dimension.CREDIT: 3,
        Dimension.EXTERNAL: 2,
    }
    assert MINIMUM_VALID_DIMENSIONS == 5
