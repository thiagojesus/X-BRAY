from fastapi import APIRouter
from fastapi.responses import JSONResponse

from macro_score.repository import (
    InvalidMacroScoreRangeError,
    query_latest_macro_score,
    query_macro_score_history,
)

router = APIRouter(prefix="/api/macro-score", tags=["macro_score"])


@router.get("")
def get_macro_score() -> JSONResponse:
    snapshot = query_latest_macro_score()
    if snapshot is None:
        return JSONResponse(content={"status": "unavailable", "data": None})
    return JSONResponse(content={
        "status": "ok" if snapshot.payload["score"] is not None else "unavailable",
        "data": snapshot.payload,
    })


@router.get("/history")
def get_macro_score_history(start: str, end: str) -> JSONResponse:
    try:
        history = query_macro_score_history(start, end)
    except InvalidMacroScoreRangeError:
        return JSONResponse(status_code=422, content={"error": "invalid_macro_score_range"})
    payloads = [snapshot.payload for snapshot in history]
    return JSONResponse(content={
        "status": "ok" if payloads else "unavailable",
        "data": payloads,
    })
