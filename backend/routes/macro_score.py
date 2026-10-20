import json
from datetime import date
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
from macro_score.retrospective import query_retrospective_history
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
        return JSONResponse(
            content={
                "status": "unavailable",
                "capabilities": {"macro_summary": capability},
                "data": None,
            }
        )
    return JSONResponse(
        content={
            "status": "ok" if snapshot.payload["score"] is not None else "unavailable",
            "capabilities": {"macro_summary": capability},
            "data": snapshot.payload,
        }
    )


@router.get("/history")
def get_macro_score_history(start: str, end: str) -> JSONResponse:
    try:
        history = query_macro_score_history(start, end)
    except InvalidMacroScoreRangeError:
        return JSONResponse(
            status_code=422, content={"error": "invalid_macro_score_range"}
        )
    payloads = [snapshot.payload for snapshot in history]
    return JSONResponse(
        content={
            "status": "ok" if payloads else "unavailable",
            "data": payloads,
        }
    )


@router.get("/summary")
def get_macro_score_summary() -> JSONResponse:
    if not macro_summary_enabled():
        return JSONResponse(status_code=404, content={"error": "summary_disabled"})
    snapshots = query_latest_macro_score_pair()
    if len(snapshots) != 2:
        return JSONResponse(
            status_code=409, content={"error": "insufficient_equivalent_cycles"}
        )
    summary_result = build_macro_score_summary(snapshots[0], snapshots[1])
    match summary_result:
        case MacroScoreSummaryAvailable(payload=payload):
            return JSONResponse(content={"status": "ok", "data": payload})
        case MacroScoreSummaryUnavailable(reason=reason):
            return JSONResponse(status_code=409, content={"error": reason.value})
    assert_never(summary_result)


@router.get("/retrospective-history")
def get_retrospective_macro_history(
    start: str = "2016-01", end: str = "2026-12"
) -> JSONResponse:
    if len(start) != 7 or len(end) != 7 or start[4:5] != "-" or end[4:5] != "-":
        return JSONResponse(
            status_code=422, content={"error": "invalid_reference_month_range"}
        )
    try:
        first = date.fromisoformat(f"{start}-01")
        last = date.fromisoformat(f"{end}-01")
    except ValueError:
        return JSONResponse(
            status_code=422, content={"error": "invalid_reference_month_range"}
        )
    if first > last or (last.year - first.year) * 12 + last.month - first.month > 120:
        return JSONResponse(
            status_code=422, content={"error": "invalid_reference_month_range"}
        )
    records = query_retrospective_history(start, end)
    counts: dict[str, set[str]] = {}
    for record in records:
        counts.setdefault(str(record["component_key"]), set()).add(
            str(record["reference_month"])
        )
    return JSONResponse(
        content={
            "status": "ok" if records else "unavailable",
            "source_basis": "retrospective_reference",
            "point_in_time": False,
            "methodology_version": "0.3.0-beta",
            "data": [
                {**record, "inputs": json.loads(str(record["inputs_json"]))}
                for record in records
            ],
            "coverage": {
                key: {
                    "distinct_months": len(months),
                    "percentile_eligible": len(months) >= 24,
                }
                for key, months in sorted(counts.items())
            },
            "di_history_available": False,
        }
    )
