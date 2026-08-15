from fastapi import APIRouter, Query
from datafetchers.bcb_sgs import fetch_sgs_series, fetch_sgs_batch, force_refresh_sgs
from config import COMPLEMENTARY

router = APIRouter(prefix="/api/complementares", tags=["complementares"])


@router.get("/")
def get_complementares():
    data = fetch_sgs_batch(COMPLEMENTARY, start_date="01/01/2015")
    return {"source": "BCB SGS", "data": data}


@router.get("/reservas")
def get_reservas(start: str = Query(None), end: str = Query(None)):
    data = fetch_sgs_series(COMPLEMENTARY["reservas_internacionais"], start_date=start, end_date=end)
    return {"series": "Reservas Internacionais", "data": data}


@router.get("/base-monetaria")
def get_base_monetaria(start: str = Query(None), end: str = Query(None)):
    data = fetch_sgs_series(COMPLEMENTARY["base_monetaria"], start_date=start, end_date=end)
    return {"series": "Base Monetária", "data": data}


@router.get("/ic-commodities")
def get_ic_commodities(start: str = Query(None), end: str = Query(None)):
    data = fetch_sgs_series(COMPLEMENTARY["ic_commodities"], start_date=start, end_date=end)
    return {"series": "IC-Br Commodities", "data": data}


@router.post("/refresh")
def refresh_complementares():
    results = {}
    for name, code in COMPLEMENTARY.items():
        results[name] = force_refresh_sgs(code)
    return {"status": "refreshed", "data": results}
