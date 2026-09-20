from decimal import Decimal
from typing import Final

from config import FISCAL
from datafetchers.bcb_sgs import force_refresh_sgs
from dataset_guard import DatasetUnavailableError
from db.cached_reads import query_sgs_batch
from domain_contracts import DatasetEvaluation, Record, SeriesMap, evaluate_series_map
from fastapi import APIRouter, Depends
from indicator_catalog import (
    INDICATOR_CATALOG,
    IndicatorKey,
    IndicatorSpec,
    Transformation,
)
from refresh_auth import require_refresh_auth

router = APIRouter(prefix="/api/fiscal", tags=["fiscal"])

SIGN_CONVENTION: Final[dict[str, str]] = {
    "dbgg_pib": "debt_positive",
    "dlsp_pib": "debt_positive",
    "saldo_primario_12m_pib": "surplus_positive",
    "juros_nominais_12m_pib": "cost_positive",
    "saldo_nominal_12m_pib": "surplus_positive",
}


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


def _present_record(record: Record, spec: IndicatorSpec) -> Record:
    value = Decimal(str(record["valor"]))
    if spec.transformation is Transformation.INVERT_SIGN:
        value = -value
    return {**record, "valor": format(value, ".2f")}


def _present_series(data: SeriesMap) -> SeriesMap:
    return {
        name: [
            _present_record(record, INDICATOR_CATALOG[IndicatorKey(name)])
            for record in records
        ]
        for name, records in data.items()
    }


def _metadata(evaluation: DatasetEvaluation) -> dict[str, dict[str, object]]:
    return {
        name: {
            "code": int(spec.provider_code),
            "unit": spec.unit.value,
            "frequency": spec.frequency.value,
            "freshness": evaluation.quality["metadata"][name]["freshness"],
            "sign_convention": SIGN_CONVENTION[name],
        }
        for name in evaluation.data
        for spec in (INDICATOR_CATALOG[IndicatorKey(name)],)
    }


def _response() -> dict[str, object]:
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
def get_fiscal() -> dict[str, object]:
    return _response()


@router.post("/refresh", dependencies=[Depends(require_refresh_auth)])
def refresh_fiscal() -> dict[str, object]:
    for code in FISCAL.values():
        force_refresh_sgs(code)
    return _response()
