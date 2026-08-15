from fastapi import APIRouter, Query
from datafetchers.bcb_sgs import fetch_sgs_series, fetch_sgs_batch, force_refresh_sgs
from config import EXCHANGE

router = APIRouter(prefix="/api/cambio", tags=["cambio"])


@router.get("")
def get_cambio():
    data = fetch_sgs_batch(EXCHANGE, start_date="01/01/2015")
    return {"source": "BCB SGS", "data": data}


@router.get("/usd")
def get_usd(start: str = Query(None), end: str = Query(None)):
    compra = fetch_sgs_series(EXCHANGE["ptax_compra_usd"], start_date=start, end_date=end)
    venda = fetch_sgs_series(EXCHANGE["ptax_venda_usd"], start_date=start, end_date=end)
    return {"series": "USD/BRL PTAX", "compra": compra, "venda": venda}


@router.get("/eur")
def get_eur(start: str = Query(None), end: str = Query(None)):
    data = fetch_sgs_series(EXCHANGE["eur_brl"], start_date=start, end_date=end)
    return {"series": "EUR/BRL PTAX", "code": EXCHANGE["eur_brl"], "data": data}


@router.post("/refresh")
def refresh_cambio():
    results = {}
    for name, code in EXCHANGE.items():
        results[name] = force_refresh_sgs(code)
    return {"status": "refreshed", "data": results}
