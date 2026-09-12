from datetime import datetime, timezone

from fastapi import APIRouter, Depends, Query
from datafetchers.bcb_sgs import read_sgs_series, read_sgs_batch, force_refresh_sgs
from config import INFLATION
from dataset_guard import DatasetUnavailableError, require_records
from domain_contracts import evaluate_series_map
from indicator_catalog import INDICATOR_CATALOG, IndicatorKey
from refresh_auth import require_refresh_auth

router = APIRouter(prefix="/api/inflacao", tags=["inflacao"])


@router.get("")
def get_inflacao():
    required = ("ipca", "ipca_12m")
    optional = tuple(name for name in INFLATION if name not in required)
    specs = {name: INDICATOR_CATALOG[IndicatorKey(name)] for name in INFLATION}
    evaluated = evaluate_series_map(
        "inflacao",
        read_sgs_batch(INFLATION),
        required,
        optional,
        specs,
        datetime.now(timezone.utc),
    )
    if not evaluated.data:
        raise DatasetUnavailableError("inflacao")
    return {"source": "BCB SGS", "data": evaluated.data}


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


@router.post("/refresh", dependencies=[Depends(require_refresh_auth)])
def refresh_inflacao():
    results = {}
    for name, code in INFLATION.items():
        results[name] = force_refresh_sgs(code)
    return {"status": "refreshed", "data": results}
