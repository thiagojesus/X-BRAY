from fastapi import APIRouter
from datafetchers.polymarket import read_state_polls, force_refresh_states
from dataset_guard import require_records

router = APIRouter(prefix="/api/eleicoes/estados", tags=["eleicoes"])


@router.get("")
def get_state_polls():
    data = read_state_polls()
    require_records("eleicoes.days", data["days"])
    require_records("eleicoes.ufs", data["ufs"])
    national = data.get("national", {})
    require_records("eleicoes.national.candidates", national.get("candidates", []))
    require_records("eleicoes.national.history", list(national.get("history", {})))
    return data


@router.post("/refresh")
def refresh_state_polls():
    payload = force_refresh_states()
    return {"status": "refreshed", **payload}
