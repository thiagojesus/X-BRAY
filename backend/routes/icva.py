from fastapi import APIRouter
from datafetchers.cielo import fetch_icva, force_refresh_icva

router = APIRouter(prefix="/api/icva", tags=["icva"])


@router.get("")
def get_icva():
    data = fetch_icva()
    return {"source": "Cielo ICVA", "data": data}


@router.get("/setores")
def get_icva_sectors():
    from datafetchers.cielo import SECTORS, MACRO_SECTORS
    return {
        "sectors": SECTORS,
        "macro_sectors": MACRO_SECTORS,
    }


@router.post("/refresh")
def refresh_icva():
    data = force_refresh_icva()
    return {"status": "refreshed", "data": data}
