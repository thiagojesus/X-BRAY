from datetime import datetime, timezone

from fastapi import APIRouter, Depends
from datafetchers.bcb_sgs import read_sgs_batch, force_refresh_sgs
from config import IPCA_GROUPS, IPCA_NATURE, IPCA_CORE, IPCA_PRICES
from dataset_guard import DatasetUnavailableError
from domain_contracts import evaluate_series_map
from indicator_catalog import INDICATOR_CATALOG, IndicatorKey
from refresh_auth import require_refresh_auth

router = APIRouter(prefix="/api/ipca-decomposicao", tags=["ipca-decomposicao"])


def _evaluate(dataset: str, codes: dict[str, int]):
    names = tuple(codes)
    specs = {name: INDICATOR_CATALOG[IndicatorKey(name)] for name in names}
    evaluated = evaluate_series_map(
        dataset,
        read_sgs_batch(codes),
        names,
        (),
        specs,
        datetime.now(timezone.utc),
        minimum_usable=1,
    )
    if not evaluated.data:
        raise DatasetUnavailableError(dataset)
    return evaluated.data


@router.get("/grupos")
def get_ipca_grupos():
    data = _evaluate("ipca.grupos", IPCA_GROUPS)
    return {"source": "BCB SGS", "type": "grupos_despesa", "data": data}


@router.get("/naturezas")
def get_ipca_naturezas():
    data = _evaluate("ipca.naturezas", IPCA_NATURE)
    return {"source": "BCB SGS", "type": "naturezas", "data": data}


@router.get("/core")
def get_ipca_core():
    data = _evaluate("ipca.nucleos", IPCA_CORE)
    return {"source": "BCB SGS", "type": "core", "data": data}


@router.get("/precos")
def get_ipca_precos():
    data = _evaluate("ipca.precos", IPCA_PRICES)
    return {"source": "BCB SGS", "type": "livres_administrados", "data": data}


@router.get("/tudo")
def get_ipca_tudo():
    grupos = _evaluate("ipca.grupos", IPCA_GROUPS)
    naturezas = _evaluate("ipca.naturezas", IPCA_NATURE)
    core = _evaluate("ipca.nucleos", IPCA_CORE)
    precos = _evaluate("ipca.precos", IPCA_PRICES)
    return {
        "source": "BCB SGS",
        "grupos": grupos,
        "naturezas": naturezas,
        "core": core,
        "precos": precos,
    }


@router.post("/refresh", dependencies=[Depends(require_refresh_auth)])
def refresh_ipca_decomposicao():
    results = {}
    all_series = {**IPCA_GROUPS, **IPCA_NATURE, **IPCA_CORE, **IPCA_PRICES}
    for name, code in all_series.items():
        results[name] = force_refresh_sgs(code)
    return {"status": "refreshed", "data": results}
