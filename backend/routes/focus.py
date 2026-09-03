from fastapi import APIRouter
from datafetchers.focus import read_focus, read_all_focus, force_refresh_focus
from dataset_guard import require_records, require_series_map

router = APIRouter(prefix="/api/focus", tags=["focus"])


@router.get("")
def get_all_focus():
    data = require_series_map("focus", read_all_focus())
    return {"source": "BCB FOCUS OData", "data": data}


@router.get("/{indicator}")
def get_focus_indicator(indicator: str):
    data = require_records(f"focus.{indicator}", read_focus(indicator))
    return {"source": "BCB FOCUS", "indicator": indicator, "data": data}


@router.post("/refresh")
def refresh_focus():
    data = force_refresh_focus()
    return {"status": "refreshed", "data": data}
