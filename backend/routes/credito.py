from datetime import date, datetime, timedelta, timezone
from typing import cast

from fastapi import APIRouter, Depends

from config import CREDIT
from credit_exposures import build_credit_exposures
from datafetchers.bcb_sgs import force_refresh_sgs
from dataset_guard import DatasetUnavailableError
from db.cached_reads import query_sgs_batch
from domain_contracts import (
    DatasetStatus,
    QualityPayload,
    SeriesMetadata,
    UnavailableSeries,
    evaluate_series_map,
    publish_quality_snapshot,
)
from indicator_catalog import INDICATOR_CATALOG, IndicatorKey
from refresh_auth import require_refresh_auth

router = APIRouter(prefix="/api/credito", tags=["credito"])

_MODALITY_KEYS = frozenset({
    "carteira_pf_cheque_especial",
    "carteira_pf_credito_pessoal_nao_consignado",
    "carteira_pf_cartao_rotativo",
    "carteira_pf_cartao_parcelado",
})


def _stock_deadline(reference: str, freshness_days: int) -> date:
    first = date.fromisoformat(reference)
    next_month = first.replace(day=28) + timedelta(days=4)
    month_end = next_month.replace(day=1) - timedelta(days=1)
    return month_end + timedelta(days=freshness_days)


def _response() -> dict[str, object]:
    specs = {name: INDICATOR_CATALOG[IndicatorKey(name)] for name in CREDIT}
    fetched = query_sgs_batch(CREDIT)
    legacy_names = tuple(name for name in CREDIT if name not in _MODALITY_KEYS)
    legacy_specs = {name: specs[name] for name in legacy_names}
    evaluated_at = datetime.now(timezone.utc)
    evaluation = evaluate_series_map(
        "credito",
        fetched,
        legacy_names,
        (),
        legacy_specs,
        now=evaluated_at,
    )
    modalities = build_credit_exposures(fetched, as_of=evaluated_at.date())
    data = dict(evaluation.data)
    metadata = {
        name: {
            "code": int(specs[name].provider_code),
            "unit": specs[name].unit.value,
            "frequency": specs[name].frequency.value,
            "reference_period": quality["latest_reference"],
            "deadline": quality["deadline"],
            "freshness": quality["freshness"],
        }
        for name, quality in evaluation.quality["metadata"].items()
    }
    unavailable = [item["series"] for item in evaluation.quality["unavailable"]]
    quality_unavailable: list[UnavailableSeries] = list(evaluation.quality["unavailable"])
    for key, modality in modalities.items():
        raw_records = cast(dict[str, object], fetched).get(key, [])
        data[key] = raw_records if isinstance(raw_records, list) else []
        latest_reference = modality["latest"]["reference"]
        quality_metadata: SeriesMetadata | None = None
        if isinstance(latest_reference, str):
            deadline = _stock_deadline(latest_reference, specs[key].freshness_days)
            modality_metadata: SeriesMetadata = {
                "latest_reference": latest_reference,
                "deadline": deadline.isoformat(),
                "freshness": "stale" if evaluated_at.date() > deadline else "ok",
                "observations": sum(point["value"] is not None for point in modality["history"]),
            }
            quality_metadata = modality_metadata
            metadata[key] = {
                "code": modality["code"], "unit": modality["unit"],
                "frequency": modality["frequency"],
                "reference_period": latest_reference,
                "deadline": deadline.isoformat(),
                "freshness": modality_metadata["freshness"],
            }
        else:
            unavailable.append(key)
            quality_metadata = None
            reason = modality["latest"]["reason"]
            quality_unavailable.append({
                "series": key,
                "required": True,
                "reason": reason if isinstance(reason, str) else "no_valid_observations",
            })
        if quality_metadata is not None:
            evaluation.quality["metadata"][key] = quality_metadata

    combined_unavailable = [*quality_unavailable]
    combined_metadata = dict(evaluation.quality["metadata"])
    available_count = len(combined_metadata)
    if available_count == 0:
        status: DatasetStatus = "unavailable"
    elif (
        combined_unavailable
        or available_count < len(CREDIT)
        or evaluation.quality["status"] != "ok"
    ):
        status = "partial"
    else:
        status = "ok"
    quality: QualityPayload = {
        "dataset": "credito",
        "status": status,
        "last_updated": max(
            (entry["latest_reference"] for entry in combined_metadata.values()),
            default=None,
        ),
        "metadata": combined_metadata,
        "unavailable": combined_unavailable,
    }
    publish_quality_snapshot(quality)
    if status == "unavailable":
        raise DatasetUnavailableError("credito")
    unavailable = [entry["series"] for entry in combined_unavailable]
    return {
        "data": data,
        "status": status,
        "unavailable": unavailable,
        "metadata": metadata,
        "modalities": modalities,
    }


@router.get("")
def get_credito():
    return _response()


@router.post("/refresh", dependencies=[Depends(require_refresh_auth)])
def refresh_credito():
    for code in CREDIT.values():
        force_refresh_sgs(code)
    return _response()
