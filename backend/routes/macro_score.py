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
from macro_score.retrospective import (
    has_verified_di_maturities,
    query_retrospective_history,
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
    eligible_months: dict[str, set[str]] = {}
    for record in records:
        component, month = str(record["component_key"]), str(record["reference_month"])
        counts.setdefault(component, set()).add(month)
        if component in {"juro_real_ex_ante_1y", "inclinacao_di_5y_1y"}:
            if has_verified_di_maturities(str(record["inputs_json"])):
                eligible_months.setdefault(component, set()).add(month)
        else:
            eligible_months.setdefault(component, set()).add(month)
    di_components = {"juro_real_ex_ante_1y", "inclinacao_di_5y_1y"}
    formula_versions = sorted({str(record["formula_version"]) for record in records})
    return JSONResponse(
        content={
            "status": "ok" if records else "unavailable",
            "source_basis": "retrospective_reference",
            "point_in_time": False,
            "methodology_version": "0.3.0-beta",
            "formula_versions": formula_versions,
            "data": [
                {
                    **record,
                    "inputs": json.loads(str(record["inputs_json"])),
                    "point_in_time": False,
                    "history_basis": "retrospective_reference",
                    "input_semantics_verified": None,
                    "eligibility": "candidate_only; maturity calendar provenance not verified"
                    if str(record["component_key"]) in di_components
                    and not has_verified_di_maturities(str(record["inputs_json"])) else None,
                }
                for record in records
            ],
            "coverage": {
                key: {
                    "distinct_months": len(months),
                    "percentile_eligible": len(eligible_months.get(key, set())) >= 24,
                    **({"eligible_distinct_months": len(eligible_months.get(key, set()))}
                       if key in di_components else {}),
                }
                for key, months in sorted(counts.items())
            },
            "di_history_available": di_components.issubset(counts),
        }
    )
