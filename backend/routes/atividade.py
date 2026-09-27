from calendar import monthrange
from datetime import date, datetime, timezone
from typing import Literal, cast

from fastapi import APIRouter, Depends, HTTPException, Query

from config import ACTIVITY, ACTIVITY_REFRESH, ACTIVITY_SECTORAL
from datafetchers.bcb_sgs import read_sgs_series, read_sgs_batch, force_refresh_sgs
from dataset_guard import DatasetUnavailableError, require_records
from db.cached_reads import query_sgs_batch
from domain_contracts import Record, SeriesMap, evaluate_series_map
from indicator_catalog import INDICATOR_CATALOG, IndicatorKey
from refresh_auth import require_refresh_auth
from series_transformations import TransformationMode, transform_series

router = APIRouter(prefix="/api/atividade", tags=["atividade"])
SectoralMode = Literal["level", "mom", "yoy"]


def _parse_date(value: str | None, field: str) -> date | None:
    if value is None:
        return None
    try:
        return datetime.strptime(value, "%d/%m/%Y").date()
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=f"{field} must use DD/MM/YYYY") from exc


def _replace_year(value: date, year: int) -> date:
    day = min(value.day, monthrange(year, value.month)[1])
    return value.replace(year=year, day=day)


def _shift_month(value: date, months: int) -> date:
    period = value.year * 12 + value.month - 1 + months
    year, zero_based_month = divmod(period, 12)
    month = zero_based_month + 1
    day = min(value.day, monthrange(year, month)[1])
    return date(year, month, day)


def _validated_window(start: str | None, end: str | None) -> tuple[date | None, date | None]:
    parsed_start = _parse_date(start, "start")
    parsed_end = _parse_date(end, "end")
    if parsed_start is not None and parsed_end is not None:
        if parsed_start > parsed_end:
            raise HTTPException(status_code=422, detail="start must not be after end")
        if parsed_end > _replace_year(parsed_start, parsed_start.year + 10):
            raise HTTPException(status_code=422, detail="window must not exceed ten years")
    return parsed_start, parsed_end


def _query_start(start: date | None, mode: SectoralMode) -> str | None:
    if start is None:
        return None
    match mode:
        case "level":
            lagged = start
        case "mom":
            lagged = _shift_month(start, -1)
        case "yoy":
            lagged = _replace_year(start, start.year - 1)
    return lagged.strftime("%d/%m/%Y")


def _inside_window(record: Record, start: date | None, end: date | None) -> bool:
    reference = datetime.strptime(str(record["data"]), "%d/%m/%Y").date()
    return (start is None or reference >= start) and (end is None or reference <= end)


def _sectoral_response(
    mode: SectoralMode,
    start: str | None,
    end: str | None,
) -> dict[str, object]:
    parsed_start, parsed_end = _validated_window(start, end)
    raw = query_sgs_batch(
        ACTIVITY_SECTORAL,
        start_date=_query_start(parsed_start, mode),
        end_date=end,
    )
    transformed: SeriesMap = {
        name: [
            cast(Record, record)
            for record in transform_series(records, cast(TransformationMode, mode))
            if _inside_window(cast(Record, record), parsed_start, parsed_end)
        ]
        for name, records in raw.items()
    }
    specs = {
        name: INDICATOR_CATALOG[IndicatorKey(name)]
        for name in ACTIVITY_SECTORAL
    }
    evaluation = evaluate_series_map(
        "atividade_setorial",
        transformed,
        tuple(ACTIVITY_SECTORAL),
        (),
        specs,
        datetime.now(timezone.utc),
    )
    if evaluation.quality["status"] == "unavailable":
        raise DatasetUnavailableError("atividade_setorial")
    usable_names = frozenset(evaluation.data)
    return {
        "source": "BCB SGS",
        "mode": mode,
        "data": {name: records for name, records in transformed.items() if name in usable_names},
        "status": evaluation.quality["status"],
        "last_updated": evaluation.quality["last_updated"],
        "unavailable": [item["series"] for item in evaluation.quality["unavailable"]],
        "metadata": {
            name: {
                "code": int(specs[name].provider_code),
                "label": specs[name].label,
                "short_label": specs[name].short_label,
                "unit": specs[name].unit.value if mode == "level" else "pct",
                "frequency": specs[name].frequency.value,
                "reference_period": quality["latest_reference"],
                "deadline": quality["deadline"],
                "freshness": quality["freshness"],
            }
            for name, quality in evaluation.quality["metadata"].items()
        },
    }


@router.get("")
def get_atividade():
    required = ("ibc_br", "desemprego")
    optional = tuple(name for name in ACTIVITY if name not in required)
    specs = {name: INDICATOR_CATALOG[IndicatorKey(name)] for name in ACTIVITY}
    evaluated = evaluate_series_map(
        "atividade",
        read_sgs_batch(ACTIVITY, start_date="01/01/2015"),
        required,
        optional,
        specs,
        datetime.now(timezone.utc),
    )
    if not evaluated.data:
        raise DatasetUnavailableError("atividade")
    return {"source": "BCB SGS", "data": evaluated.data}


@router.get("/setorial")
def get_atividade_setorial(
    mode: SectoralMode = Query("level"),
    start: str | None = Query(None),
    end: str | None = Query(None),
):
    return _sectoral_response(mode, start, end)


@router.get("/pib")
def get_pib(start: str = Query(None), end: str = Query(None)):
    data = require_records(
        "atividade.pib",
        read_sgs_series(ACTIVITY["pib"], start_date=start, end_date=end),
    )
    return {"series": "PIB nominal anual", "code": ACTIVITY["pib"], "data": data}


@router.get("/ibc-br")
def get_ibc_br(start: str = Query(None), end: str = Query(None)):
    data = require_records(
        "atividade.ibc_br",
        read_sgs_series(ACTIVITY["ibc_br"], start_date=start, end_date=end),
    )
    return {"series": "IBC-Br", "code": ACTIVITY["ibc_br"], "data": data}


@router.get("/desemprego")
def get_desemprego(start: str = Query(None), end: str = Query(None)):
    data = require_records(
        "atividade.desemprego",
        read_sgs_series(ACTIVITY["desemprego"], start_date=start, end_date=end),
    )
    return {"series": "Desemprego PNAD", "code": ACTIVITY["desemprego"], "data": data}


@router.post("/refresh", dependencies=[Depends(require_refresh_auth)])
def refresh_atividade():
    results = {}
    for name, code in ACTIVITY_REFRESH.items():
        results[name] = force_refresh_sgs(code)
    return {"status": "refreshed", "data": results}
