import os
from pathlib import Path

BASE_DIR = Path(__file__).parent
CACHE_DIR = BASE_DIR / "cache"
CACHE_DIR.mkdir(exist_ok=True)

BCB_SGS_BASE = "https://api.bcb.gov.br/dados/serie/bcdata.sgs.{code}/dados"
BCB_ODATA_BASE = "https://olinda.bcb.gov.br/olinda/servico/Expectativas/versao/v1/odata"
ANBIMA_XLS_URL = "https://s3-data-prd-use1-precos.s3.us-east-1.amazonaws.com/arquivos/indices-historico/IMAB-HISTORICO.xls"
CIELO_BLOG_BASE = "https://blog.cielo.com.br/indice-icva"

# IPCA series codes
IPCA_GROUPS = {
    "alimentacao_bebidas": 1635,
    "habitacao": 1636,
    "artigos_residencia": 1637,
    "vestuario": 1638,
    "transportes": 1639,
    "comunicacao": 1640,
    "saude_cuidados": 1641,
    "despesas_pessoais": 1642,
}

IPCA_NATURE = {
    "bens_duraveis": 10843,
    "bens_semi_duraveis": 10842,
    "bens_nao_duraveis": 10841,
    "servicos": 10844,
}

IPCA_CORE = {
    "core_ex1": 1621,
    "core_medias_aparadas": 4466,
    "core_dp": 16122,
}

IPCA_PRICES = {
    "itens_livres": 11428,
    "transacionaveis": 4447,
    "nao_transacionaveis": 4448,
    "administrados": 4449,
}

INTEREST_RATES = {
    "selic_meta": 432,
    "selic_efetiva": 11,
    "cdi": 12,
    "tr": 226,
}

INFLATION = {
    "ipca": 433,
    "ipca_12m": 13522,
    "ipca_15": 7478,
    "inpc": 188,
    "igpm": 189,
    "igpdi": 190,
}

ACTIVITY = {
    "pib": 21337,
    "ibc_br": 24363,
    "desemprego": 18771,
}

EXCHANGE = {
    "ptax_compra_usd": 1,
    "ptax_venda_usd": 10813,
    "eur_brl": 21619,
}

COMPLEMENTARY = {
    "reservas_internacionais": 138,
    "base_monetaria": 1880,
    "ic_commodities": 27574,
}
