from fastapi import APIRouter, Query
from datafetchers.bcb_sgs import fetch_sgs_batch, force_refresh_sgs
from config import IPCA_GROUPS, IPCA_NATURE, IPCA_CORE, IPCA_PRICES

router = APIRouter(prefix="/api/ipca-decomposicao", tags=["ipca-decomposicao"])


@router.get("/grupos")
def get_ipca_grupos():
    data = fetch_sgs_batch(IPCA_GROUPS)
    return {"source": "BCB SGS", "type": "grupos_despesa", "data": data}


@router.get("/naturezas")
def get_ipca_naturezas():
    data = fetch_sgs_batch(IPCA_NATURE)
    return {"source": "BCB SGS", "type": "naturezas", "data": data}


@router.get("/core")
def get_ipca_core():
    data = fetch_sgs_batch(IPCA_CORE)
    return {"source": "BCB SGS", "type": "core", "data": data}


@router.get("/precos")
def get_ipca_precos():
    data = fetch_sgs_batch(IPCA_PRICES)
    return {"source": "BCB SGS", "type": "livres_administrados", "data": data}


@router.get("/tudo")
def get_ipca_tudo():
    grupos = fetch_sgs_batch(IPCA_GROUPS)
    naturezas = fetch_sgs_batch(IPCA_NATURE)
    core = fetch_sgs_batch(IPCA_CORE)
    precos = fetch_sgs_batch(IPCA_PRICES)
    return {
        "source": "BCB SGS",
        "grupos": grupos,
        "naturezas": naturezas,
        "core": core,
        "precos": precos,
    }


@router.post("/refresh")
def refresh_ipca_decomposicao():
    results = {}
    all_series = {**IPCA_GROUPS, **IPCA_NATURE, **IPCA_CORE, **IPCA_PRICES}
    for name, code in all_series.items():
        results[name] = force_refresh_sgs(code)
    return {"status": "refreshed", "data": results}
