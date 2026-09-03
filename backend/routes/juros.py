from fastapi import APIRouter, Query
from datafetchers.bcb_sgs import read_sgs_series, read_sgs_batch, force_refresh_sgs
from config import INTEREST_RATES
from dataset_guard import require_records, require_series_map

router = APIRouter(prefix="/api/juros", tags=["juros"])


@router.get("")
def get_juros():
    data_selic = read_sgs_batch({k: v for k, v in INTEREST_RATES.items() if k != "tr"}, start_date="01/01/2015")
    data_tr = read_sgs_batch({"tr": INTEREST_RATES["tr"]}, start_date="01/01/1991")
    require_series_map("juros", data_selic)
    require_series_map("juros.tr", data_tr)
    merged = {**data_selic, **data_tr}
    return {"source": "BCB SGS", "data": merged}


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
        read_sgs_series(INTEREST_RATES["tr"], start_date=start, end_date=end),
    )
    return {"series": "TR", "code": INTEREST_RATES["tr"], "data": data}


@router.post("/refresh")
def refresh_juros():
    results = {}
    for name, code in INTEREST_RATES.items():
        results[name] = force_refresh_sgs(code)
    return {"status": "refreshed", "data": results}
