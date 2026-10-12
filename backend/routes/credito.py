from datetime import datetime, timezone

from fastapi import APIRouter, Depends

from config import CREDIT
from datafetchers.bcb_sgs import force_refresh_sgs
from dataset_guard import DatasetUnavailableError
from db.cached_reads import query_sgs_batch
from domain_contracts import evaluate_series_map
from indicator_catalog import INDICATOR_CATALOG, IndicatorKey
from refresh_auth import require_refresh_auth

router = APIRouter(prefix="/api/credito", tags=["credito"])


def _response() -> dict:
    specs = {name: INDICATOR_CATALOG[IndicatorKey(name)] for name in CREDIT}
    evaluation = evaluate_series_map(
        "credito",
        query_sgs_batch(CREDIT),
        tuple(CREDIT),
        (),
        specs,
        now=datetime.now(timezone.utc),
    )
    if evaluation.quality["status"] == "unavailable":
        raise DatasetUnavailableError("credito")
    return {
        "data": evaluation.data,
        "status": evaluation.quality["status"],
        "unavailable": [item["series"] for item in evaluation.quality["unavailable"]],
        "metadata": {
            name: {
                "code": int(specs[name].provider_code),
                "unit": specs[name].unit.value,
                "frequency": specs[name].frequency.value,
                "reference_period": quality["latest_reference"],
                "deadline": quality["deadline"],
                "freshness": quality["freshness"],
            }
            for name, quality in evaluation.quality["metadata"].items()
        },
    }


@router.get("")
def get_credito():
    return _response()


@router.post("/refresh", dependencies=[Depends(require_refresh_auth)])
def refresh_credito():
    for code in CREDIT.values():
        force_refresh_sgs(code)
    return _response()
