from datetime import datetime, timezone

from fastapi import APIRouter, Depends, Query
from datafetchers.bcb_sgs import read_sgs_series, read_sgs_batch, force_refresh_sgs
from config import EXCHANGE
from dataset_guard import DatasetUnavailableError, require_records
from domain_contracts import evaluate_series_map
from indicator_catalog import INDICATOR_CATALOG, IndicatorKey
from refresh_auth import require_refresh_auth

router = APIRouter(prefix="/api/cambio", tags=["cambio"])


@router.get("")
def get_cambio():
    required = ("ptax_venda_usd",)
    optional = tuple(name for name in EXCHANGE if name not in required)
    specs = {name: INDICATOR_CATALOG[IndicatorKey(name)] for name in EXCHANGE}
    evaluated = evaluate_series_map(
        "cambio",
        read_sgs_batch(EXCHANGE, start_date="01/01/2015"),
        required,
        optional,
        specs,
        datetime.now(timezone.utc),
    )
    if not evaluated.data:
        raise DatasetUnavailableError("cambio")
    return {"source": "BCB SGS", "data": evaluated.data}


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


@router.post("/refresh", dependencies=[Depends(require_refresh_auth)])
def refresh_cambio():
    results = {}
    for name, code in EXCHANGE.items():
        results[name] = force_refresh_sgs(code)
    return {"status": "refreshed", "data": results}
