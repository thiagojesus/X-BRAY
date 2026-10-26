"""Source-audit inventory. This module contains identifiers, not ingestion parsers."""

from types import SimpleNamespace
from typing import TypeAlias

from config import (
    ACTIVITY_REFRESH,
    COMPLEMENTARY_REFRESH,
    CREDIT,
    EXCHANGE,
    FISCAL,
    INFLATION,
    INTEREST_RATES,
    IPCA_CORE,
    IPCA_GROUPS,
    IPCA_NATURE,
    IPCA_PRICES,
    SETOR_EXTERNO,
)
from indicator_catalog import INDICATOR_CATALOG

# Independently maintained official identifiers; intentionally not imported
# from the ingestion producer catalog/parser.
SIDRA_DATASETS = {
    "pim_industria": SimpleNamespace(key="pim_industria", table_id=8888, variable_id=12607, classification_id=544, category_id=129314, territory_code="1", territory_level="N1", unit="index", unit_code="30", unit_label="Número-índice", base_version="2022=100", category_label="1 Indústria geral", frequency="monthly"),
    "pmc_varejo_ampliado": SimpleNamespace(key="pmc_varejo_ampliado", table_id=8881, variable_id=7170, classification_id=11046, category_id=56736, territory_code="1", territory_level="N1", unit="index", unit_code="30", unit_label="Número-índice", base_version="2022=100", category_label="Índice de volume de vendas no comércio varejista ampliado", frequency="monthly"),
    "pms_servicos": SimpleNamespace(key="pms_servicos", table_id=5906, variable_id=7168, classification_id=11046, category_id=56726, territory_code="1", territory_level="N1", unit="index", unit_code="30", unit_label="Número-índice", base_version="2022=100", category_label="Índice de volume de serviços", frequency="monthly"),
    "massa_rendimento_real": SimpleNamespace(key="massa_rendimento_real", table_id=6392, variable_id=6293, classification_id=None, category_id=None, territory_code="1", territory_level="N1", unit="brl_million", unit_code="1550", unit_label="Milhões de Reais", base_version="real_deflator_latest_published_moving_quarter", category_label=None, frequency="moving_quarter"),
    "taxa_subutilizacao": SimpleNamespace(key="taxa_subutilizacao", table_id=6441, variable_id=4118, classification_id=None, category_id=None, territory_code="1", territory_level="N1", unit="pct", unit_code="2", unit_label="Percentual", base_version=None, category_label=None, frequency="moving_quarter"),
}

SGS_GROUPS = {
    "juros": INTEREST_RATES,
    "inflacao": INFLATION,
    "atividade": ACTIVITY_REFRESH,
    "fiscal": FISCAL,
    "credito": CREDIT,
    "cambio": EXCHANGE,
    "setor_externo": SETOR_EXTERNO,
    "complementares": COMPLEMENTARY_REFRESH,
    "ipca_grupos": IPCA_GROUPS,
    "ipca_naturezas": IPCA_NATURE,
    "ipca_nucleos": IPCA_CORE,
    "ipca_precos": IPCA_PRICES,
}

SGS_CODES = tuple(sorted({int(code) for group in SGS_GROUPS.values() for code in group.values()}))
Inventory: TypeAlias = dict[str, object]

FOCUS_ANNUAL = {
    "IPCA": "IPCA",
    "Selic": "Selic",
    "PIB": "PIB Total",
    "Câmbio": "Câmbio",
    "IGP-M": "IGP-M",
}
FOCUS_12M = "IPCA_12M_S_BASE0"
FOCUS_ANNUAL_HORIZON_OFFSETS = (0, 1, 2, 3, 4)
FOCUS_ANNUAL_BASES = (0, 1)
FOCUS_CONTRACT_VERSION = "source-audit-focus-contract-v2"
CATALOG_VERSION = "source-audit-catalog-v3"

ANBIMA_URL = "https://s3-data-prd-use1-precos.s3.us-east-1.amazonaws.com/arquivos/indices-historico/IMAB-HISTORICO.xls"
TREASURY_URL = (
    "https://www.tesourotransparente.gov.br/ckan/dataset/taxas-dos-titulos-ofertados-pelo-tesouro-direto/"
    "resource/796d2059-14e9-44e3-80c9-2d9e30b405c1/download"
)
B3_URL = "https://www.b3.com.br/pesquisapregao/download?filelist=SPRD{YYMMDD}.zip"


def inventory() -> Inventory:
    """Build machine-readable expected coverage from maintained static definitions."""
    code_groups: dict[int, list[str]] = {}
    for group_name, mapping in SGS_GROUPS.items():
        for code in mapping.values():
            code_groups.setdefault(int(code), []).append(group_name)
    sgs: list[dict[str, object]] = []
    for code in SGS_CODES:
        spec = next((item for item in INDICATOR_CATALOG.values() if item.provider_code == code), None)
        sgs.append({
            "key": str(spec.key) if spec else f"sgs_{code}",
            "provider": "bcb_sgs",
            "provider_code": code,
            "refresh_groups": sorted(code_groups.get(code, [])),
            "unit": spec.unit.value if spec else "catalog_missing",
            "source_url": f"https://api.bcb.gov.br/dados/serie/bcdata.sgs.{code}/dados/ultimos/5?formato=json",
            "database": "public.sgs",
            "database_key": f"series_code={code}",
        })
    sidra: list[dict[str, object]] = []
    for key, spec in SIDRA_DATASETS.items():
        url = (f"https://apisidra.ibge.gov.br/values/t/{spec.table_id}/n1/{spec.territory_code}"
               f"/v/{spec.variable_id}/p/last?formato=json")
        if spec.classification_id is not None and spec.category_id is not None:
            url = url.replace("?formato=json", f"/c{spec.classification_id}/{spec.category_id}?formato=json")
        sidra.append({
            "key": key, "provider": "ibge_sidra", "source_url": url,
            "table_id": spec.table_id, "variable_id": spec.variable_id,
            "classification_id": spec.classification_id, "category_id": spec.category_id,
            "territory_level": spec.territory_level, "territory_code": spec.territory_code,
            "unit": spec.unit, "frequency": spec.frequency,
            "database": "public.sidra_observations",
        })
    return {
        "version": CATALOG_VERSION,
        "sgs": sgs,
        "focus_annual": [{"key": key, "indicator": value, "source": "ExpectativasMercadoAnuais", "database": "public.focus"}
                         for key, value in FOCUS_ANNUAL.items()],
        "focus_contract": {
            "version": FOCUS_CONTRACT_VERSION,
            "as_of_basis": "database_snapshot_utc_year_fallback_audit_utc_year",
            "annual_horizon_offsets": list(FOCUS_ANNUAL_HORIZON_OFFSETS),
            "annual_bases": list(FOCUS_ANNUAL_BASES),
            "annual_identity_dimensions": ["indicator", "target_year", "baseCalculo"],
            "rolling_12m": {"indicator": "IPCA", "base_calculation": 0, "smoothed": "S"},
        },
        "focus_12m": {"key": FOCUS_12M, "source": "ExpectativasMercadoInflacao12Meses", "database": "public.focus",
                      "filter": "Indicador eq 'IPCA' and Suavizada eq 'S' and baseCalculo eq 0"},
        "sidra": sidra,
        "anbima": {"key": "IMA-B", "source_url": ANBIMA_URL, "database": "public.anbima"},
        "treasury": {"key": "tesouro-direto", "source_url": TREASURY_URL, "database": "public.treasury"},
        "b3_di": {"key": "B3-DI-curve", "source_url_template": B3_URL, "database": "public.b3_di",
                  "identity_database": "public.b3_di_instrument_snapshots"},
        "excluded": ["Polymarket (election ended; retired from application and refresh)"],
    }
