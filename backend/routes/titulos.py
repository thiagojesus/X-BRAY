from fastapi import APIRouter
from datafetchers.anbima import read_anbima_ima, force_refresh_anbima
from dataset_guard import require_records, require_series_map

router = APIRouter(prefix="/api/titulos", tags=["titulos"])


@router.get("")
def get_titulos():
    data = require_series_map("anbima", read_anbima_ima())
    return {"source": "ANBIMA IMA Historical XLS", "data": data}


@router.get("/ima")
def get_ima():
    data = require_series_map("anbima", read_anbima_ima())
    return {"source": "ANBIMA", "data": data}


@router.get("/nntn-b")
def get_imab_chart():
    data = read_anbima_ima()
    if not isinstance(data, dict) or "historico" not in data:
        require_records("anbima.historico", [])

    rows = data["historico"]
    chart_data = []
    for r in rows:
        idx_name = r.get("Índice", "")
        if idx_name not in ("IMA-B", "IMA - B"):
            continue
        chart_data.append({
            "data": r.get("Data de Referência", ""),
            "indice": r.get("Número Índice"),
            "variacao_diaria": r.get("Variação Diária (%)"),
            "variacao_12m": r.get("Variação 12 Meses (%)"),
            "duration": r.get("Duration (d.u.)"),
            "pmr": r.get("PMR"),
        })

    require_records("anbima.ima_b", chart_data)
    return {
        "source": "ANBIMA IMA",
        "title": "IMA-B (NTN-B Principal)",
        "description": "Índice que acompanha o desempenho dos títulos NTN-B do Tesouro Nacional, indexados ao IPCA.",
        "data": chart_data,
    }


@router.post("/refresh")
def refresh_titulos():
    data = force_refresh_anbima()
    return {"status": "refreshed", "data": data}
