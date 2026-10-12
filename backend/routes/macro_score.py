from typing import assert_never

from fastapi import APIRouter
from fastapi.responses import JSONResponse

from config import macro_summary_enabled
from macro_score.repository import (
    InvalidMacroScoreRangeError,
    query_latest_macro_score,
    query_latest_macro_score_pair,
    query_macro_score_history,
)
from macro_score.summary import (
    MacroScoreSummaryAvailable,
    MacroScoreSummaryUnavailable,
    build_macro_score_summary,
)

router = APIRouter(prefix="/api/macro-score", tags=["macro_score"])


@router.get("")
def get_macro_score() -> JSONResponse:
    capability = macro_summary_enabled()
    snapshot = query_latest_macro_score()
    if snapshot is None:
        return JSONResponse(content={
            "status": "unavailable",
            "capabilities": {"macro_summary": capability},
            "data": None,
        })
    return JSONResponse(content={
        "status": "ok" if snapshot.payload["score"] is not None else "unavailable",
        "capabilities": {"macro_summary": capability},
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


@router.get("/summary")
def get_macro_score_summary() -> JSONResponse:
    if not macro_summary_enabled():
        return JSONResponse(status_code=404, content={"error": "summary_disabled"})
    snapshots = query_latest_macro_score_pair()
    if len(snapshots) != 2:
        return JSONResponse(status_code=409, content={"error": "insufficient_equivalent_cycles"})
    summary_result = build_macro_score_summary(snapshots[0], snapshots[1])
    match summary_result:
        case MacroScoreSummaryAvailable(payload=payload):
            return JSONResponse(content={"status": "ok", "data": payload})
        case MacroScoreSummaryUnavailable(reason=reason):
            return JSONResponse(status_code=409, content={"error": reason.value})
    assert_never(summary_result)
