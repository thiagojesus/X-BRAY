from datetime import datetime, timezone

from fastapi import APIRouter, Depends
from datafetchers.focus import read_focus, read_all_focus, force_refresh_focus
from dataset_guard import DatasetUnavailableError, require_records
from domain_contracts import StaticFreshnessSpec, evaluate_series_map
from indicator_catalog import Frequency
from refresh_auth import require_refresh_auth

router = APIRouter(prefix="/api/focus", tags=["focus"])


@router.get("")
def get_all_focus():
    required = ("IPCA", "Selic")
    optional = ("PIB", "Câmbio", "IGP-M")
    specs = {
        name: StaticFreshnessSpec(Frequency.MONTHLY, 75)
        for name in (*required, *optional)
    }
    evaluated = evaluate_series_map(
        "focus",
        read_all_focus(),
        required,
        optional,
        specs,
        datetime.now(timezone.utc),
    )
    if not evaluated.data:
        raise DatasetUnavailableError("focus")
    return {"source": "BCB FOCUS OData", "data": evaluated.data}


@router.get("/{indicator}")
def get_focus_indicator(indicator: str):
    data = require_records(f"focus.{indicator}", read_focus(indicator))
    return {"source": "BCB FOCUS", "indicator": indicator, "data": data}


@router.post("/refresh", dependencies=[Depends(require_refresh_auth)])
def refresh_focus():
    data = force_refresh_focus()
    return {"status": "refreshed", "data": data}
