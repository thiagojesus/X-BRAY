from fastapi import APIRouter, Query
from datafetchers.bcb_sgs import fetch_sgs_series, fetch_sgs_batch, force_refresh_sgs
from config import ACTIVITY

router = APIRouter(prefix="/api/atividade", tags=["atividade"])


@router.get("")
def get_atividade():
    data = fetch_sgs_batch(ACTIVITY, start_date="01/01/2015")
    return {"source": "BCB SGS", "data": data}


@router.get("/pib")
def get_pib(start: str = Query(None), end: str = Query(None)):
    data = fetch_sgs_series(ACTIVITY["pib"], start_date=start, end_date=end)
    return {"series": "PIB variação trimestral", "code": ACTIVITY["pib"], "data": data}


@router.get("/ibc-br")
def get_ibc_br(start: str = Query(None), end: str = Query(None)):
    data = fetch_sgs_series(ACTIVITY["ibc_br"], start_date=start, end_date=end)
    return {"series": "IBC-Br", "code": ACTIVITY["ibc_br"], "data": data}


@router.get("/desemprego")
def get_desemprego(start: str = Query(None), end: str = Query(None)):
    data = fetch_sgs_series(ACTIVITY["desemprego"], start_date=start, end_date=end)
    return {"series": "Desemprego PNAD", "code": ACTIVITY["desemprego"], "data": data}


@router.post("/refresh")
def refresh_atividade():
    results = {}
    for name, code in ACTIVITY.items():
        results[name] = force_refresh_sgs(code)
    return {"status": "refreshed", "data": results}
