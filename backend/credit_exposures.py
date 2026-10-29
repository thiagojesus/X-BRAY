"""Read-time derived measures for selected, nominal free-credit PF balances."""

from __future__ import annotations

import re
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import date
from math import isfinite
from typing import Literal, TypedDict, cast

from indicator_catalog import INDICATOR_CATALOG, IndicatorKey

ExposureReason = Literal[
    "no_valid_observations", "invalid_current", "missing_current", "missing_base",
    "zero_base", "invalid_base", "invalid_result",
]
_KEYS = (
    "carteira_pf_cheque_especial",
    "carteira_pf_credito_pessoal_nao_consignado",
    "carteira_pf_cartao_rotativo",
    "carteira_pf_cartao_parcelado",
)
_NUMBER = re.compile(r"^[+-]?(?:\d+(?:[.,]\d*)?|[.,]\d+)(?:[eE][+-]?\d+)?$")


class GrowthMetric(TypedDict):
    value_pct: float | None
    current_reference: str | None
    base_reference: str | None
    base_observation_reference: str | None
    reason: ExposureReason | None


class ExposureHistoryPoint(TypedDict):
    reference: str
    value: float | None
    mom_pct: float | None
    yoy_pct: float | None
    mom_base_reference: str | None
    yoy_base_reference: str | None
    mom_reason: ExposureReason | None
    yoy_reason: ExposureReason | None


class ExposurePayload(TypedDict):
    code: int
    label: str
    official_label: str
    short_label: str
    source_url: str
    frequency: str
    unit: str
    universe: str
    measure: str
    interpretation: str
    latest: dict[str, str | float | None]
    mom: GrowthMetric
    yoy: GrowthMetric
    history: list[ExposureHistoryPoint]
    unavailable: bool


@dataclass(frozen=True, slots=True)
class _Observation:
    reference: date
    value: float


def _reference(raw: object) -> date | None:
    if not isinstance(raw, str) or not raw:
        return None
    try:
        if re.fullmatch(r"\d{2}/\d{2}/\d{4}", raw):
            day, month, year = (int(part) for part in raw.split("/"))
            return date(year, month, day)
        return date.fromisoformat(raw)
    except ValueError:
        return None


def _value(raw: object) -> float | None:
    if isinstance(raw, bool) or not isinstance(raw, (str, int, float)):
        return None
    text = str(raw).strip()
    if not _NUMBER.fullmatch(text):
        return None
    try:
        parsed = float(text.replace(",", "."))
    except ValueError:
        return None
    if not isfinite(parsed) or parsed < 0:
        return None
    return parsed


def _observations(
    records: Sequence[object], as_of: date,
) -> tuple[dict[date, _Observation], set[date], bool]:
    by_month: dict[date, _Observation] = {}
    invalid_months: set[date] = set()
    saw_invalid = False
    for record in records:
        if not isinstance(record, Mapping):
            saw_invalid = True
            continue
        fields = cast(Mapping[str, object], record)
        reference = _reference(fields.get("data", fields.get("Data")))
        value = _value(fields.get("valor", fields.get("value")))
        if reference is None:
            saw_invalid = True
            continue
        month = reference.replace(day=1)
        if reference > as_of or value is None:
            invalid_months.add(month)
            saw_invalid = True
            continue
        current = by_month.get(month)
        # For a monthly stock, use the latest dated point in that month.
        # Stable input order breaks same-day ties (the store is date-unique).
        if current is None or reference >= current.reference:
            by_month[month] = _Observation(reference, value)
    return by_month, invalid_months, saw_invalid


def _growth(
    values: Mapping[date, _Observation], current_month: date, offset: int,
    *, current_invalid: bool = False, invalid_months: set[date] | None = None,
) -> GrowthMetric:
    current = values.get(current_month)
    base_month = _month_shift(current_month, -offset)
    base = values.get(base_month)
    current_reference = current.reference.isoformat() if current else None
    base_reference = base_month.isoformat()
    base_observation_reference = base.reference.isoformat() if base else None
    if current is None:
        reason: ExposureReason = "invalid_current" if current_invalid else "missing_current"
        return {"value_pct": None, "current_reference": current_reference,
                "base_reference": base_reference,
                "base_observation_reference": base_observation_reference, "reason": reason}
    if base is None:
        return {"value_pct": None, "current_reference": current_reference,
                "base_reference": base_reference, "base_observation_reference": None,
                "reason": "invalid_base" if invalid_months and base_month in invalid_months else "missing_base"}
    if base.value == 0:
        return {"value_pct": None, "current_reference": current_reference,
                "base_reference": base_reference,
                "base_observation_reference": base_observation_reference, "reason": "zero_base"}
    result = 100.0 * (current.value / base.value - 1.0)
    if not isfinite(result):
        return {"value_pct": None, "current_reference": current_reference,
                "base_reference": base_reference,
                "base_observation_reference": base_observation_reference,
                "reason": "invalid_result"}
    return {"value_pct": result, "current_reference": current_reference,
            "base_reference": base_reference,
            "base_observation_reference": base_observation_reference, "reason": None}


def _month_shift(month: date, offset: int) -> date:
    ordinal = month.year * 12 + month.month - 1 + offset
    year, zero_based_month = divmod(ordinal, 12)
    return date(year, zero_based_month + 1, 1)


def _history(
    values: Mapping[date, _Observation], invalid_months: set[date],
) -> list[ExposureHistoryPoint]:
    if not values:
        return []
    first, last = min(values), max(values)
    points: list[ExposureHistoryPoint] = []
    month = first
    while month <= last:
        observation = values.get(month)
        mom = _growth(
            values, month, 1, current_invalid=month in invalid_months,
            invalid_months=invalid_months,
        )
        yoy = _growth(
            values, month, 12, current_invalid=month in invalid_months,
            invalid_months=invalid_months,
        )
        points.append({
            "reference": month.isoformat(),
            "value": observation.value if observation else None,
            "mom_pct": mom["value_pct"], "yoy_pct": yoy["value_pct"],
            "mom_base_reference": mom["base_reference"],
            "yoy_base_reference": yoy["base_reference"],
            "mom_reason": mom["reason"], "yoy_reason": yoy["reason"],
        })
        month = _month_shift(month, 1)
    return points


def _payload(key: str, records: Sequence[object], as_of: date) -> ExposurePayload:
    spec = INDICATOR_CATALOG[IndicatorKey(key)]
    values, invalid_months, invalid = _observations(records, as_of)
    if values:
        latest_month = max(values)
        observation = values[latest_month]
        latest: dict[str, str | float | None] = {
            "reference": observation.reference.isoformat(), "value": observation.value,
            "reason": None,
        }
        mom = _growth(values, latest_month, 1, invalid_months=invalid_months)
        yoy = _growth(values, latest_month, 12, invalid_months=invalid_months)
    else:
        latest_month = None
        latest = {"reference": None, "value": None,
                  "reason": "invalid_current" if invalid else "no_valid_observations"}
        unavailable_reason: ExposureReason = "invalid_current" if invalid else "no_valid_observations"
        mom: GrowthMetric = {"value_pct": None, "current_reference": None,
               "base_reference": None, "base_observation_reference": None,
               "reason": unavailable_reason}
        yoy: GrowthMetric = {"value_pct": None, "current_reference": None,
               "base_reference": None, "base_observation_reference": None,
               "reason": unavailable_reason}
    return {
        "code": int(spec.provider_code), "label": spec.short_label,
        "official_label": spec.label, "short_label": spec.short_label,
        "source_url": spec.source_url, "frequency": spec.frequency.value,
        "unit": spec.unit.value, "universe": "Pessoas físicas — recursos livres",
        "measure": "Saldo nominal devedor no fim do mês",
        "interpretation": "Exposição nominal selecionada; aumento não mede, isoladamente, deterioração de crédito.",
        "latest": latest, "mom": mom, "yoy": yoy,
        "history": _history(values, invalid_months), "unavailable": latest_month is None,
    }


def build_credit_exposures(
    records_by_key: Mapping[str, object],
    *,
    as_of: date,
) -> dict[str, ExposurePayload]:
    """Build response projections without mutating cached raw observations."""
    result: dict[str, ExposurePayload] = {}
    for key in _KEYS:
        records = records_by_key.get(key, ())
        safe_records = (
            cast(Sequence[object], records)
            if isinstance(records, Sequence) and not isinstance(records, (str, bytes))
            else ()
        )
        payload = _payload(key, safe_records, as_of)
        if safe_records is not records:
            payload["latest"]["reason"] = "invalid_current"
            payload["mom"]["reason"] = "invalid_current"
            payload["yoy"]["reason"] = "invalid_current"
        result[key] = payload
    return result
