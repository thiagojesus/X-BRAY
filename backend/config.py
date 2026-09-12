import os
from pathlib import Path

from indicator_catalog import CatalogGroup, legacy_codes

BASE_DIR = Path(__file__).parent
CACHE_DIR = BASE_DIR / "cache"
try:
    CACHE_DIR.mkdir(exist_ok=True)
except OSError:
    CACHE_DIR = Path("/tmp") / "xbray-cache"
    CACHE_DIR.mkdir(exist_ok=True)

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
EXCHANGE = legacy_codes(CatalogGroup.EXCHANGE)
COMPLEMENTARY = legacy_codes(CatalogGroup.COMPLEMENTARY)
