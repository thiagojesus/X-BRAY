from datetime import datetime, timezone
from typing import TypedDict

from fastapi import APIRouter, Depends

from config import SETOR_EXTERNO
from datafetchers.bcb_sgs import force_refresh_sgs
from dataset_guard import DatasetUnavailableError
from db.cached_reads import query_sgs_batch
from domain_contracts import (
    DatasetEvaluation,
    DatasetStatus,
    SeriesMap,
    evaluate_series_map,
)
from indicator_catalog import INDICATOR_CATALOG, IndicatorKey
from refresh_auth import require_refresh_auth

router = APIRouter(prefix="/api/setor-externo", tags=["setor-externo"])


class ExternalMetadata(TypedDict):
    code: int
    title: str
    short_label: str
    unit: str
    frequency: str
    source_url: str
    reference_period: str
    deadline: str
    freshness: str


class ExternalResponse(TypedDict):
    data: SeriesMap
    status: DatasetStatus
    unavailable: list[str]
    metadata: dict[str, ExternalMetadata]


def _evaluate_external() -> DatasetEvaluation:
    required = tuple(SETOR_EXTERNO)
    return evaluate_series_map(
        "setor_externo",
        query_sgs_batch(SETOR_EXTERNO),
        required=required,
        optional=(),
        specs={name: INDICATOR_CATALOG[IndicatorKey(name)] for name in required},
        now=datetime.now(timezone.utc),
    )


def _metadata(evaluation: DatasetEvaluation) -> dict[str, ExternalMetadata]:
    return {
        name: {
            "code": int(spec.provider_code),
            "title": spec.label,
            "short_label": spec.short_label,
            "unit": spec.unit.value,
            "frequency": spec.frequency.value,
            "source_url": spec.source_url,
            "reference_period": quality["latest_reference"],
            "deadline": quality["deadline"],
            "freshness": quality["freshness"],
        }
        for name, quality in evaluation.quality["metadata"].items()
        for spec in (INDICATOR_CATALOG[IndicatorKey(name)],)
    }


def _response() -> ExternalResponse:
    evaluation = _evaluate_external()
    if evaluation.quality["status"] == "unavailable":
        raise DatasetUnavailableError("setor_externo")
    return {
        "data": evaluation.data,
        "status": evaluation.quality["status"],
        "unavailable": [item["series"] for item in evaluation.quality["unavailable"]],
        "metadata": _metadata(evaluation),
    }


@router.get("")
def get_setor_externo() -> ExternalResponse:
    return _response()


@router.post("/refresh", dependencies=[Depends(require_refresh_auth)])
def refresh_setor_externo() -> ExternalResponse:
    for code in SETOR_EXTERNO.values():
        force_refresh_sgs(code)
    return _response()
