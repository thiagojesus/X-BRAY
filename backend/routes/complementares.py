from datetime import datetime, timezone

from fastapi import APIRouter, Depends, Query
from datafetchers.bcb_sgs import read_sgs_series, read_sgs_batch, force_refresh_sgs
from db.store import get_meta
from config import COMPLEMENTARY
from dataset_guard import DatasetUnavailableError, require_records
from domain_contracts import evaluate_series_map
from indicator_catalog import INDICATOR_CATALOG, IndicatorKey
from refresh_auth import require_refresh_auth

router = APIRouter(prefix="/api/complementares", tags=["complementares"])


@router.get("")
def get_complementares():
    required = ("m0", "m1", "m2", "ic_commodities")
    optional = tuple(name for name in COMPLEMENTARY if name not in required)
    specs = {name: INDICATOR_CATALOG[IndicatorKey(name)] for name in COMPLEMENTARY}
    evaluated = evaluate_series_map(
        "complementares",
        read_sgs_batch(COMPLEMENTARY, start_date="01/01/2015"),
        required,
        optional,
        specs,
        datetime.now(timezone.utc),
    )
    if not evaluated.data:
        raise DatasetUnavailableError("complementares")
    return {"source": "BCB SGS", "last_updated": get_meta("last_successful_update"), "data": evaluated.data}


@router.get("/reservas")
def get_reservas(start: str = Query(None), end: str = Query(None)):
    data = require_records(
        "complementares.reservas_internacionais",
        read_sgs_series(COMPLEMENTARY["reservas_internacionais"], start_date=start, end_date=end),
    )
    return {"series": "Reservas Internacionais", "data": data}


@router.get("/base-monetaria")
def get_base_monetaria(start: str = Query(None), end: str = Query(None)):
    data = require_records(
        "complementares.base_monetaria",
        read_sgs_series(COMPLEMENTARY["base_monetaria"], start_date=start, end_date=end),
    )
    return {"series": "Base Monetária", "data": data}


@router.get("/ic-commodities")
def get_ic_commodities(start: str = Query(None), end: str = Query(None)):
    data = require_records(
        "complementares.ic_commodities",
        read_sgs_series(COMPLEMENTARY["ic_commodities"], start_date=start, end_date=end),
    )
    return {"series": "IC-Br Commodities", "data": data}


@router.post("/refresh", dependencies=[Depends(require_refresh_auth)])
def refresh_complementares():
    results = {}
    for name, code in COMPLEMENTARY.items():
        results[name] = force_refresh_sgs(code)
    return {"status": "refreshed", "data": results}
