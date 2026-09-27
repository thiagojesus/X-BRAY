from fastapi import APIRouter
from fastapi.responses import JSONResponse

from derived.models import result_to_payload
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
