from fastapi import APIRouter, Query
from datafetchers.bcb_sgs import read_sgs_series, read_sgs_batch, force_refresh_sgs
from config import INFLATION
from dataset_guard import require_records, require_series_map

router = APIRouter(prefix="/api/inflacao", tags=["inflacao"])


@router.get("")
def get_inflacao():
    data = require_series_map("inflacao", read_sgs_batch(INFLATION))
    return {"source": "BCB SGS", "data": data}


@router.get("/ipca")
def get_ipca(start: str = Query(None), end: str = Query(None)):
    data = require_records(
        "inflacao.ipca",
        read_sgs_series(INFLATION["ipca"], start_date=start, end_date=end),
    )
    return {"series": "IPCA Mensal", "code": INFLATION["ipca"], "data": data}


@router.get("/ipca-12m")
def get_ipca_12m(start: str = Query(None), end: str = Query(None)):
    data = require_records(
        "inflacao.ipca_12m",
        read_sgs_series(INFLATION["ipca_12m"], start_date=start, end_date=end),
    )
    return {"series": "IPCA Acumulado 12 meses", "code": INFLATION["ipca_12m"], "data": data}


@router.get("/igpm")
def get_igpm(start: str = Query(None), end: str = Query(None)):
    data = require_records(
        "inflacao.igpm",
        read_sgs_series(INFLATION["igpm"], start_date=start, end_date=end),
    )
    return {"series": "IGP-M", "code": INFLATION["igpm"], "data": data}


@router.post("/refresh")
def refresh_inflacao():
    results = {}
    for name, code in INFLATION.items():
        results[name] = force_refresh_sgs(code)
    return {"status": "refreshed", "data": results}
