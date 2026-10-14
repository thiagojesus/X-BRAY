from datetime import date, datetime, timedelta, timezone
from typing import TypedDict
from zoneinfo import ZoneInfo

from fastapi import APIRouter
from fastapi.responses import JSONResponse

from derived.models import DerivedResultPayload, result_to_payload, utc_datetime
from derived.real_rate import REAL_RATE_KEY
from derived.repository import (
    DerivedSnapshot,
    query_latest_snapshots,
    query_snapshot_history_window,
)

router = APIRouter(prefix="/api/derivados", tags=["derivados"])
_ADDITIONAL_KEYS = frozenset({
    "cobertura_externa",
    "variacao_saldo_primario_12m",
    "spread_credito_pf_selic",
    "distancia_focus_meta",
    "inclinacao_di_5y_1y",
})
_HISTORY_KEYS = (
    REAL_RATE_KEY,
    "cobertura_externa",
    "variacao_saldo_primario_12m",
    "spread_credito_pf_selic",
    "distancia_focus_meta",
    "inclinacao_di_5y_1y",
)


class DerivedHistoryPoint(TypedDict):
    month: str
    snapshot: DerivedResultPayload


def _shift_month(month: date, offset: int) -> date:
    month_index = month.year * 12 + month.month - 1 + offset
    return date(month_index // 12, month_index % 12 + 1, 1)


@router.get("/history")
def get_derived_history() -> JSONResponse:
    cutoff = utc_datetime(datetime.now(timezone.utc))
    local_today = cutoff.astimezone(ZoneInfo("America/Sao_Paulo")).date()
    end_month = date(local_today.year, local_today.month, 1)
    start_month = _shift_month(end_month, -23)
    end_exclusive = local_today + timedelta(days=1)
    history: dict[str, list[DerivedHistoryPoint]] = {}

    for key in _HISTORY_KEYS:
        monthly: dict[str, DerivedSnapshot] = {}
        snapshots = query_snapshot_history_window(
            key,
            start_reference_period=start_month,
            end_reference_period=end_exclusive,
            as_of=cutoff,
        )
        for snapshot in snapshots:
            reference_date = snapshot.result.reference_date
            if reference_date is not None:
                month = reference_date[:7]
                monthly.setdefault(month, snapshot)
        history[key] = [
            {"month": month, "snapshot": result_to_payload(snapshot.result)}
            for month, snapshot in sorted(monthly.items())
        ]

    return JSONResponse(content={
        "status": "ok" if any(history.values()) else "unavailable",
        "window": {
            "start_month": start_month.strftime("%Y-%m"),
            "end_month": end_month.strftime("%Y-%m"),
            "cutoff": cutoff.isoformat(),
        },
        "data": history,
    })


@router.get("")
def get_derivados() -> JSONResponse:
    snapshots = query_latest_snapshots()
    data = {}
    for key, snapshot in snapshots.items():
        payload = result_to_payload(snapshot.result)
        data[key] = (
            {**payload, "unavailable_reason": payload["reason"]}
            if key in _ADDITIONAL_KEYS
            else payload
        )
    return JSONResponse(content={
        "status": "ok" if snapshots else "unavailable",
        "data": data,
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
