from fastapi import APIRouter
from fastapi.responses import JSONResponse

from derived.models import result_to_payload
from derived.real_rate import REAL_RATE_KEY
from derived.repository import query_latest_snapshots

router = APIRouter(prefix="/api/derivados", tags=["derivados"])


@router.get("")
def get_derivados() -> JSONResponse:
    snapshots = query_latest_snapshots()
    return JSONResponse(content={
        "status": "ok" if snapshots else "unavailable",
        "data": {
            key: result_to_payload(snapshot.result)
            for key, snapshot in snapshots.items()
        },
    })


@router.get("/juro-real-ex-ante-1y")
def get_real_rate_one_year() -> JSONResponse:
    snapshot = query_latest_snapshots().get(REAL_RATE_KEY)
    if snapshot is None:
        return JSONResponse(content={"status": "unavailable", "data": {}})
    payload = dict(result_to_payload(snapshot.result))
    payload["unavailable_reason"] = payload.pop("reason")
    payload.setdefault("reference_date", None)
    payload.setdefault("historical_percentile", None)
    payload.setdefault("classification", None)
    payload.setdefault("classification_reason", None)
    return JSONResponse(content={
        "status": "ok" if snapshot.result.value is not None else "unavailable",
        "data": payload,
    })
