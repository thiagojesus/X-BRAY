from fastapi import APIRouter, Query
from datetime import date
from datafetchers.b3_di import fetch_di_curves, force_refresh_di

router = APIRouter(prefix="/api/curvas-di", tags=["curvas-di"])


def _fmt(iso: str) -> str:
    d = date.fromisoformat(iso)
    return d.strftime("%d/%m/%Y")


@router.get("")
def get_curvas_di(days: int = Query(30, ge=1, le=60)):
    curves = fetch_di_curves(days=days)
    return {
        "source": "B3 Price Report (SPR)",
        "days": days,
        "dates": [_fmt(d) for d in curves.keys()],
        "curves": {_fmt(d): pts for d, pts in curves.items()},
    }


@router.post("/refresh")
def refresh_curvas_di(days: int = Query(30, ge=1, le=60)):
    curves = force_refresh_di(days=days)
    return {
        "status": "refreshed",
        "source": "B3 Price Report (SPR)",
        "days": days,
        "dates": [_fmt(d) for d in curves.keys()],
        "curves": {_fmt(d): pts for d, pts in curves.items()},
    }