from fastapi import APIRouter, Query
from datafetchers.tesouro_direto import (
    fetch_yield_curve,
    fetch_treasury_quotes,
    fetch_bond_catalog,
    fetch_bond_history,
)

router = APIRouter(prefix="/api/tesouro-direto", tags=["tesouro-direto"])


@router.get("")
def get_treasury_bonds():
    quotes = fetch_treasury_quotes()
    prefixado = [q for q in quotes if q["indexer"] == "prefixado"]
    ipca = [q for q in quotes if q["indexer"] == "ipca"]
    return {
        "source": "Tesouro Direto",
        "data": {
            "prefixado": prefixado,
            "ipca": ipca,
        },
    }


@router.get("/curva")
def get_yield_curve():
    curve = fetch_yield_curve()
    return {"source": "Tesouro Direto", "data": curve}


@router.get("/ipca")
def get_ipca_bonds():
    quotes = fetch_treasury_quotes()
    bonds = [q for q in quotes if q["indexer"] == "ipca"]
    return {"source": "Tesouro Direto", "data": bonds}


@router.get("/prefixado")
def get_prefixado_bonds():
    quotes = fetch_treasury_quotes()
    bonds = [q for q in quotes if q["indexer"] == "prefixado"]
    return {"source": "Tesouro Direto", "data": bonds}


@router.get("/titulos")
def get_bond_catalog():
    catalog = fetch_bond_catalog()
    return {"source": "Tesouro Direto", "data": catalog}


@router.get("/historico")
def get_bond_history(
    code: int = Query(...),
    days: int = Query(30, ge=0, le=9999),
):
    history = fetch_bond_history(code, days)
    if history is None:
        return {"source": "Tesouro Direto", "error": "No data available"}
    return {"source": "Tesouro Direto", "data": history}