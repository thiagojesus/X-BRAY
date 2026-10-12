from collections.abc import Mapping

from fastapi import APIRouter, Depends
from fastapi.encoders import jsonable_encoder
from fastapi.responses import JSONResponse

from datafetchers.ibge_sidra import SIDRA_DATASETS, refresh_sidra_data
from dataset_guard import DatasetUnavailableError
from db.sidra_store import query_sidra_current
from refresh_auth import require_refresh_auth

router = APIRouter(prefix="/api/atividade/sidra", tags=["atividade"])


def _metadata(key: str, latest: Mapping[str, object]) -> dict[str, object]:
    spec = SIDRA_DATASETS[key]
    classification = (
        {"id": spec.classification_id, "label": spec.classification_label}
        if spec.classification_id is not None
        else None
    )
    category = (
        {"id": spec.category_id, "label": spec.category_label}
        if spec.category_id is not None
        else None
    )
    return {
        "table_id": spec.table_id,
        "variable_id": spec.variable_id,
        "classification_id": spec.classification_id,
        "category_id": spec.category_id,
        "table": {"id": spec.table_id, "label": spec.table_label},
        "variable": {"id": spec.variable_id, "label": spec.variable_label},
        "classification": classification,
        "category": category,
        "territory": {"level": spec.territory_level, "code": spec.territory_code, "label": "Brasil"},
        "unit": spec.unit,
        "unit_label": spec.unit_label,
        "base_version": spec.base_version,
        "frequency": spec.frequency,
        "reference_period": latest["reference_period"],
        "period_label": latest["reference_label"],
        "available_at": latest["available_at"],
    }


def _public_observation(row: Mapping[str, object]) -> dict[str, object]:
    return {
        "reference_period": row["reference_period"],
        "period_label": row["reference_label"],
        "value": row["value"],
        "available_at": row["available_at"],
        "payload_hash": row["payload_hash"],
    }


@router.get("")
def get_atividade_sidra() -> JSONResponse:
    keys = tuple(SIDRA_DATASETS)
    current = query_sidra_current(keys)
    usable = {key: rows for key, rows in current.items() if rows}
    if not usable:
        raise DatasetUnavailableError("atividade_sidra")
    unavailable = [key for key in keys if key not in usable]
    latest_by_key = {
        key: max(rows, key=lambda row: str(row["reference_period"]))
        for key, rows in usable.items()
    }
    return JSONResponse(content=jsonable_encoder({
        "source": "IBGE SIDRA",
        "status": "ok" if not unavailable else "partial",
        "last_updated": max(str(row["available_at"]) for row in latest_by_key.values()),
        "data": {
            key: [_public_observation(row) for row in rows]
            for key, rows in usable.items()
        },
        "unavailable": unavailable,
        "metadata": {
            key: _metadata(key, latest)
            for key, latest in latest_by_key.items()
        },
    }))


@router.post("/refresh", dependencies=[Depends(require_refresh_auth)])
def refresh_atividade_sidra() -> JSONResponse:
    return JSONResponse(content=jsonable_encoder({"status": "refreshed", "data": refresh_sidra_data()}))
