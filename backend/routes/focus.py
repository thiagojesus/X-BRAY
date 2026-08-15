from fastapi import APIRouter
from datafetchers.focus import fetch_focus, fetch_all_focus, force_refresh_focus

router = APIRouter(prefix="/api/focus", tags=["focus"])


@router.get("")
def get_all_focus():
    data = fetch_all_focus()
    return {"source": "BCB FOCUS OData", "data": data}


@router.get("/{indicator}")
def get_focus_indicator(indicator: str):
    data = fetch_focus(indicator)
    return {"source": "BCB FOCUS", "indicator": indicator, "data": data}


@router.post("/refresh")
def refresh_focus():
    data = force_refresh_focus()
    return {"status": "refreshed", "data": data}
