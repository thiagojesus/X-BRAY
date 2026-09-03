from fastapi import APIRouter, Query
from datafetchers.bcb_sgs import read_sgs_series, read_sgs_batch, force_refresh_sgs
from config import EXCHANGE
from dataset_guard import require_records, require_series_map

router = APIRouter(prefix="/api/cambio", tags=["cambio"])


@router.get("")
def get_cambio():
    data = require_series_map("cambio", read_sgs_batch(EXCHANGE, start_date="01/01/2015"))
    return {"source": "BCB SGS", "data": data}


@router.get("/usd")
def get_usd(start: str = Query(None), end: str = Query(None)):
    compra = read_sgs_series(EXCHANGE["ptax_compra_usd"], start_date=start, end_date=end)
    venda = read_sgs_series(EXCHANGE["ptax_venda_usd"], start_date=start, end_date=end)
    require_records("cambio.usd.compra", compra)
    require_records("cambio.usd.venda", venda)
    return {"series": "USD/BRL PTAX", "compra": compra, "venda": venda}


@router.get("/eur")
def get_eur(start: str = Query(None), end: str = Query(None)):
    data = require_records(
        "cambio.eur",
        read_sgs_series(EXCHANGE["eur_brl"], start_date=start, end_date=end),
    )
    return {"series": "EUR/BRL PTAX", "code": EXCHANGE["eur_brl"], "data": data}


@router.post("/refresh")
def refresh_cambio():
    results = {}
    for name, code in EXCHANGE.items():
        results[name] = force_refresh_sgs(code)
    return {"status": "refreshed", "data": results}
