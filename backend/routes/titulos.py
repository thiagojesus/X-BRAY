from fastapi import APIRouter
from datafetchers.anbima import fetch_anbima_ima, force_refresh_anbima

router = APIRouter(prefix="/api/titulos", tags=["titulos"])


@router.get("")
def get_titulos():
    data = fetch_anbima_ima()
    return {"source": "ANBIMA IMA Historical XLS", "data": data}


@router.get("/ima")
def get_ima():
    data = fetch_anbima_ima()
    return {"source": "ANBIMA", "data": data}


@router.post("/refresh")
def refresh_titulos():
    data = force_refresh_anbima()
    return {"status": "refreshed", "data": data}
