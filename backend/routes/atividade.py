from datetime import datetime, timezone

from fastapi import APIRouter, Depends, Query
from datafetchers.bcb_sgs import read_sgs_series, read_sgs_batch, force_refresh_sgs
from config import ACTIVITY
from dataset_guard import DatasetUnavailableError, require_records
from domain_contracts import evaluate_series_map
from indicator_catalog import INDICATOR_CATALOG, IndicatorKey
from refresh_auth import require_refresh_auth

router = APIRouter(prefix="/api/atividade", tags=["atividade"])


@router.get("")
def get_atividade():
    required = ("ibc_br", "desemprego")
    optional = tuple(name for name in ACTIVITY if name not in required)
    specs = {name: INDICATOR_CATALOG[IndicatorKey(name)] for name in ACTIVITY}
    evaluated = evaluate_series_map(
        "atividade",
        read_sgs_batch(ACTIVITY, start_date="01/01/2015"),
        required,
        optional,
        specs,
        datetime.now(timezone.utc),
    )
    if not evaluated.data:
        raise DatasetUnavailableError("atividade")
    return {"source": "BCB SGS", "data": evaluated.data}


@router.get("/pib")
def get_pib(start: str = Query(None), end: str = Query(None)):
    data = require_records(
        "atividade.pib",
        read_sgs_series(ACTIVITY["pib"], start_date=start, end_date=end),
    )
    return {"series": "PIB nominal anual", "code": ACTIVITY["pib"], "data": data}


@router.get("/ibc-br")
def get_ibc_br(start: str = Query(None), end: str = Query(None)):
    data = require_records(
        "atividade.ibc_br",
        read_sgs_series(ACTIVITY["ibc_br"], start_date=start, end_date=end),
    )
    return {"series": "IBC-Br", "code": ACTIVITY["ibc_br"], "data": data}


@router.get("/desemprego")
def get_desemprego(start: str = Query(None), end: str = Query(None)):
    data = require_records(
        "atividade.desemprego",
        read_sgs_series(ACTIVITY["desemprego"], start_date=start, end_date=end),
    )
    return {"series": "Desemprego PNAD", "code": ACTIVITY["desemprego"], "data": data}


@router.post("/refresh", dependencies=[Depends(require_refresh_auth)])
def refresh_atividade():
    results = {}
    for name, code in ACTIVITY.items():
        results[name] = force_refresh_sgs(code)
    return {"status": "refreshed", "data": results}
