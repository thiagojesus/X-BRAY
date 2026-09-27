from pathlib import Path

from indicator_catalog import CatalogGroup, legacy_codes

BASE_DIR = Path(__file__).parent
_cache_dir = BASE_DIR / "cache"
try:
    _cache_dir.mkdir(exist_ok=True)
except OSError:
    _cache_dir = Path("/tmp") / "xbray-cache"
    _cache_dir.mkdir(exist_ok=True)
CACHE_DIR = _cache_dir

BCB_SGS_BASE = "https://api.bcb.gov.br/dados/serie/bcdata.sgs.{code}/dados"
BCB_ODATA_BASE = "https://olinda.bcb.gov.br/olinda/servico/Expectativas/versao/v1/odata"
ANBIMA_XLS_URL = "https://s3-data-prd-use1-precos.s3.us-east-1.amazonaws.com/arquivos/indices-historico/IMAB-HISTORICO.xls"

IPCA_GROUPS = legacy_codes(CatalogGroup.IPCA_GROUPS)
IPCA_NATURE = legacy_codes(CatalogGroup.IPCA_NATURE)
IPCA_CORE = legacy_codes(CatalogGroup.IPCA_CORE)
IPCA_PRICES = legacy_codes(CatalogGroup.IPCA_PRICES)
INTEREST_RATES = legacy_codes(CatalogGroup.INTEREST_RATES)
INFLATION = legacy_codes(CatalogGroup.INFLATION)
ACTIVITY = legacy_codes(CatalogGroup.ACTIVITY)
ACTIVITY_SECTORAL = legacy_codes(CatalogGroup.ACTIVITY_SECTORAL)
ACTIVITY_REFRESH = {**ACTIVITY, **ACTIVITY_SECTORAL}
FISCAL = legacy_codes(CatalogGroup.FISCAL)
CREDIT = legacy_codes(CatalogGroup.CREDIT)
EXCHANGE = legacy_codes(CatalogGroup.EXCHANGE)
SETOR_EXTERNO = legacy_codes(CatalogGroup.EXTERNAL)
COMPLEMENTARY_REFRESH = legacy_codes(CatalogGroup.COMPLEMENTARY)
COMPLEMENTARY = {
    "reservas_internacionais": SETOR_EXTERNO["reservas_internacionais"],
    **COMPLEMENTARY_REFRESH,
}
