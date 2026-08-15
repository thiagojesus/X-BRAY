from fastapi import APIRouter, Query
from datafetchers.bcb_sgs import fetch_sgs_series, fetch_sgs_batch, force_refresh_sgs
from config import INTEREST_RATES

router = APIRouter(prefix="/api/juros", tags=["juros"])


@router.get("/")
def get_juros():
    data = fetch_sgs_batch(INTEREST_RATES, start_date="01/01/2015")
    return {"source": "BCB SGS", "data": data}


@router.get("/selic-meta")
def get_selic_meta(
    start: str = Query(None, description="DD/MM/YYYY"),
    end: str = Query(None, description="DD/MM/YYYY"),
):
    data = fetch_sgs_series(INTEREST_RATES["selic_meta"], start_date=start, end_date=end)
    return {"series": "Selic Meta", "code": INTEREST_RATES["selic_meta"], "data": data}


@router.get("/selic-efetiva")
def get_selic_efetiva(
    start: str = Query(None),
    end: str = Query(None),
):
    data = fetch_sgs_series(INTEREST_RATES["selic_efetiva"], start_date=start, end_date=end)
    return {"series": "Selic Efetiva", "code": INTEREST_RATES["selic_efetiva"], "data": data}


@router.get("/cdi")
def get_cdi(
    start: str = Query(None),
    end: str = Query(None),
):
    data = fetch_sgs_series(INTEREST_RATES["cdi"], start_date=start, end_date=end)
    return {"series": "CDI", "code": INTEREST_RATES["cdi"], "data": data}


@router.get("/tr")
def get_tr(
    start: str = Query(None),
    end: str = Query(None),
):
    data = fetch_sgs_series(INTEREST_RATES["tr"], start_date=start, end_date=end)
    return {"series": "TR", "code": INTEREST_RATES["tr"], "data": data}


@router.post("/refresh")
def refresh_juros():
    results = {}
    for name, code in INTEREST_RATES.items():
        results[name] = force_refresh_sgs(code)
    return {"status": "refreshed", "data": results}
