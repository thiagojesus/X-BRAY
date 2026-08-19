from fastapi import APIRouter
from datafetchers.polymarket import fetch_state_polls, force_refresh_states

router = APIRouter(prefix="/api/eleicoes/estados", tags=["eleicoes"])


@router.get("")
def get_state_polls():
    return fetch_state_polls(use_cache=True)


@router.post("/refresh")
def refresh_state_polls():
    payload = force_refresh_states()
    return {"status": "refreshed", **payload}