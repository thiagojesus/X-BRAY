from datetime import datetime, timezone

from fastapi import APIRouter, Depends, Query
from datafetchers.bcb_sgs import read_sgs_series, read_sgs_batch, force_refresh_sgs
from config import INTEREST_RATES
from dataset_guard import DatasetUnavailableError, require_records
from domain_contracts import evaluate_series_map
from indicator_catalog import INDICATOR_CATALOG, IndicatorKey
from refresh_auth import require_refresh_auth
from db.store import query_tr_periods

router = APIRouter(prefix="/api/juros", tags=["juros"])


@router.get("")
def get_juros():
    data_selic = read_sgs_batch({k: v for k, v in INTEREST_RATES.items() if k != "tr"}, start_date="01/01/2015")
    data_tr = read_sgs_batch({"tr": INTEREST_RATES["tr"]}, start_date="01/01/1991")
    data_tr["tr"] = query_tr_periods(start_date="01/01/1991")
    merged = {**data_selic, **data_tr}
    required = tuple(INTEREST_RATES)
    specs = {name: INDICATOR_CATALOG[IndicatorKey(name)] for name in required}
    evaluated = evaluate_series_map("juros", merged, required, (), specs, datetime.now(timezone.utc))
    if not evaluated.data:
        raise DatasetUnavailableError("juros")
    return {"source": "BCB SGS", "data": evaluated.data}


@router.get("/selic-meta")
def get_selic_meta(
    start: str = Query(None, description="DD/MM/YYYY"),
    end: str = Query(None, description="DD/MM/YYYY"),
):
    data = require_records(
        "juros.selic_meta",
        read_sgs_series(INTEREST_RATES["selic_meta"], start_date=start, end_date=end),
    )
    return {"series": "Selic Meta", "code": INTEREST_RATES["selic_meta"], "data": data}


@router.get("/selic-efetiva")
def get_selic_efetiva(
    start: str = Query(None),
    end: str = Query(None),
):
    data = require_records(
        "juros.selic_efetiva",
        read_sgs_series(INTEREST_RATES["selic_efetiva"], start_date=start, end_date=end),
    )
    return {"series": "Selic Efetiva", "code": INTEREST_RATES["selic_efetiva"], "data": data}


@router.get("/cdi")
def get_cdi(
    start: str = Query(None),
    end: str = Query(None),
):
    data = require_records(
        "juros.cdi",
        read_sgs_series(INTEREST_RATES["cdi"], start_date=start, end_date=end),
    )
    return {"series": "CDI", "code": INTEREST_RATES["cdi"], "data": data}


@router.get("/tr")
def get_tr(
    start: str = Query(None),
    end: str = Query(None),
):
    data = require_records(
        "juros.tr",
        query_tr_periods(start_date=start, end_date=end),
    )
    return {"series": "TR", "code": INTEREST_RATES["tr"], "data": data}


@router.post("/refresh", dependencies=[Depends(require_refresh_auth)])
def refresh_juros():
    results = {}
    for name, code in INTEREST_RATES.items():
        results[name] = force_refresh_sgs(code)
    return {"status": "refreshed", "data": results}
