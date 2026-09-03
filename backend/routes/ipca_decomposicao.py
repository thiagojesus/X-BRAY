from fastapi import APIRouter, Query
from datafetchers.bcb_sgs import read_sgs_batch, force_refresh_sgs
from config import IPCA_GROUPS, IPCA_NATURE, IPCA_CORE, IPCA_PRICES
from dataset_guard import require_series_map

router = APIRouter(prefix="/api/ipca-decomposicao", tags=["ipca-decomposicao"])


@router.get("/grupos")
def get_ipca_grupos():
    data = require_series_map("ipca.grupos", read_sgs_batch(IPCA_GROUPS))
    return {"source": "BCB SGS", "type": "grupos_despesa", "data": data}


@router.get("/naturezas")
def get_ipca_naturezas():
    data = require_series_map("ipca.naturezas", read_sgs_batch(IPCA_NATURE))
    return {"source": "BCB SGS", "type": "naturezas", "data": data}


@router.get("/core")
def get_ipca_core():
    data = require_series_map("ipca.nucleos", read_sgs_batch(IPCA_CORE))
    return {"source": "BCB SGS", "type": "core", "data": data}


@router.get("/precos")
def get_ipca_precos():
    data = require_series_map("ipca.precos", read_sgs_batch(IPCA_PRICES))
    return {"source": "BCB SGS", "type": "livres_administrados", "data": data}


@router.get("/tudo")
def get_ipca_tudo():
    grupos = require_series_map("ipca.grupos", read_sgs_batch(IPCA_GROUPS))
    naturezas = require_series_map("ipca.naturezas", read_sgs_batch(IPCA_NATURE))
    core = require_series_map("ipca.nucleos", read_sgs_batch(IPCA_CORE))
    precos = require_series_map("ipca.precos", read_sgs_batch(IPCA_PRICES))
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
