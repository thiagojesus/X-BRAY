from typing import TypedDict

from fastapi import APIRouter, Depends

from config import FISCAL
from datafetchers.bcb_sgs import force_refresh_sgs
from dataset_guard import DatasetUnavailableError
from db.cached_reads import query_sgs_batch
from domain_contracts import (
    DatasetEvaluation,
    DatasetStatus,
    SeriesMap,
    evaluate_series_map,
)
from domains.fiscal import SIGN_CONVENTION, SignConvention, present_record
from indicator_catalog import (
    INDICATOR_CATALOG,
    IndicatorKey,
)
from refresh_auth import require_refresh_auth

router = APIRouter(prefix="/api/fiscal", tags=["fiscal"])


class FiscalMetadata(TypedDict):
    code: int
    unit: str
    frequency: str
    freshness: str
    sign_convention: SignConvention


class FiscalResponse(TypedDict):
    source: str
    last_updated: str | None
    status: DatasetStatus
    data: SeriesMap
    metadata: dict[str, FiscalMetadata]
    unavailable: list[str]


def _evaluate_fiscal() -> DatasetEvaluation:
    required = tuple(FISCAL)
    cached = query_sgs_batch(FISCAL)
    return evaluate_series_map(
        "fiscal",
        cached,
        required=required,
        optional=(),
        specs={name: INDICATOR_CATALOG[IndicatorKey(name)] for name in required},
    )


def _present_series(data: SeriesMap) -> SeriesMap:
    return {
        name: [
            present_record(record, INDICATOR_CATALOG[IndicatorKey(name)])
            for record in records
        ]
        for name, records in data.items()
    }


def _metadata(evaluation: DatasetEvaluation) -> dict[str, FiscalMetadata]:
    return {
        name: {
            "code": int(spec.provider_code),
            "unit": spec.unit.value,
            "frequency": spec.frequency.value,
            "freshness": evaluation.quality["metadata"][name]["freshness"],
            "sign_convention": SIGN_CONVENTION[IndicatorKey(name)],
        }
        for name in evaluation.data
        for spec in (INDICATOR_CATALOG[IndicatorKey(name)],)
    }


def _response() -> FiscalResponse:
    evaluation = _evaluate_fiscal()
    if evaluation.quality["status"] == "unavailable":
        raise DatasetUnavailableError("fiscal")
    return {
        "source": "BCB SGS",
        "last_updated": evaluation.quality["last_updated"],
        "status": evaluation.quality["status"],
        "data": _present_series(evaluation.data),
        "metadata": _metadata(evaluation),
        "unavailable": [item["series"] for item in evaluation.quality["unavailable"]],
    }


@router.get("")
def get_fiscal() -> FiscalResponse:
    return _response()


@router.post("/refresh", dependencies=[Depends(require_refresh_auth)])
def refresh_fiscal() -> FiscalResponse:
    for code in FISCAL.values():
        force_refresh_sgs(code)
    return _response()
