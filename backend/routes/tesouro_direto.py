from fastapi import APIRouter, Query
from datafetchers.tesouro_direto import (
    read_bond_catalog,
    read_bond_history,
    read_bonds_history,
    read_treasury_quotes,
    read_yield_curve,
)
from dataset_guard import require_records, require_series_map

router = APIRouter(prefix="/api/tesouro-direto", tags=["tesouro-direto"])


@router.get("")
def get_treasury_bonds():
    quotes = require_records("tesouro.cotacoes", read_treasury_quotes())
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
    curve = require_series_map("tesouro.curva", read_yield_curve())
    return {"source": "Tesouro Direto", "data": curve}


@router.get("/ipca")
def get_ipca_bonds():
    quotes = require_records("tesouro.cotacoes", read_treasury_quotes())
    bonds = [q for q in quotes if q["indexer"] == "ipca"]
    return {"source": "Tesouro Direto", "data": require_records("tesouro.ipca", bonds)}


@router.get("/prefixado")
def get_prefixado_bonds():
    quotes = require_records("tesouro.cotacoes", read_treasury_quotes())
    bonds = [q for q in quotes if q["indexer"] == "prefixado"]
    return {"source": "Tesouro Direto", "data": require_records("tesouro.prefixado", bonds)}


@router.get("/titulos")
def get_bond_catalog():
    catalog = require_records("tesouro.catalogo", read_bond_catalog())
    return {"source": "Tesouro Direto", "data": catalog}


@router.get("/historico")
def get_bond_history(
    code: int = Query(...),
    days: int = Query(30, ge=0, le=9999),
):
    history = read_bond_history(code, days)
    if history is None:
        require_records("tesouro.historico", [])
    return {"source": "Tesouro Direto", "data": history}


@router.get("/comparar")
def get_bonds_history_comparison(
    codes: str = Query(...),
    days: int = Query(90, ge=0, le=9999),
):
    try:
        code_list = [int(c.strip()) for c in codes.split(",") if c.strip()]
    except ValueError:
        return {"source": "Tesouro Direto", "data": []}
    if not code_list:
        return {"source": "Tesouro Direto", "data": []}
    histories = require_records("tesouro.comparacao", read_bonds_history(code_list[:8], days))
    return {"source": "Tesouro Direto", "data": histories}
