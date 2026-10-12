from collections.abc import Mapping
from dataclasses import dataclass
from enum import StrEnum, unique
from math import isfinite
from types import MappingProxyType
from typing import Final, TypeAlias, TypedDict

from derived.models import SnapshotOrigin
from macro_score.repository import MacroScoreSnapshot


@unique
class SummaryDirection(StrEnum):
    MORE_FAVORABLE = "more_favorable"
    LESS_FAVORABLE = "less_favorable"
    STABLE = "stable"


@unique
class SummaryUnavailableReason(StrEnum):
    INSUFFICIENT_EQUIVALENT_CYCLES = "insufficient_equivalent_cycles"


class DimensionDeltaPayload(TypedDict):
    key: str
    label: str
    previous_score: float
    current_score: float
    delta: float


class MacroScoreSummaryPayload(TypedDict):
    template_version: str
    previous_score_month: str
    current_score_month: str
    previous_score: float
    current_score: float
    score_delta: float
    direction: str
    improving_dimensions: list[DimensionDeltaPayload]
    worsening_dimensions: list[DimensionDeltaPayload]
    summary: str


@dataclass(frozen=True, slots=True)
class MacroScoreSummaryAvailable:
    payload: MacroScoreSummaryPayload


@dataclass(frozen=True, slots=True)
class MacroScoreSummaryUnavailable:
    reason: SummaryUnavailableReason


MacroScoreSummaryResult: TypeAlias = MacroScoreSummaryAvailable | MacroScoreSummaryUnavailable

_DIMENSION_LABELS: Final[Mapping[str, str]] = MappingProxyType({
    "activity": "Atividade",
    "inflation": "Inflação",
    "monetary": "Monetária",
    "fiscal": "Fiscal",
    "credit": "Crédito",
    "external": "Externo",
})
_SUMMARY_TEMPLATE_VERSION: Final = "0.1.0"
_UNAVAILABLE = MacroScoreSummaryUnavailable(
    SummaryUnavailableReason.INSUFFICIENT_EQUIVALENT_CYCLES
)


def _available_component_keys(snapshot: MacroScoreSnapshot) -> frozenset[str]:
    return frozenset(
        key
        for key, component in snapshot.payload["components"].items()
        if component["value"] is not None
    )


def _dimension_deltas(
    current: MacroScoreSnapshot,
    previous: MacroScoreSnapshot,
) -> tuple[list[DimensionDeltaPayload], list[DimensionDeltaPayload]]:
    improving: list[DimensionDeltaPayload] = []
    worsening: list[DimensionDeltaPayload] = []
    current_dimensions = current.payload["dimensions"]
    previous_dimensions = previous.payload["dimensions"]
    for key in sorted(current_dimensions.keys() & previous_dimensions.keys()):
        current_score = current_dimensions[key]["score"]
        previous_score = previous_dimensions[key]["score"]
        if current_score is None or previous_score is None:
            continue
        delta = current_score - previous_score
        change: DimensionDeltaPayload = {
            "key": key,
            "label": _DIMENSION_LABELS[key],
            "previous_score": previous_score,
            "current_score": current_score,
            "delta": delta,
        }
        if delta > 0:
            improving.append(change)
        elif delta < 0:
            worsening.append(change)
    improving.sort(key=lambda item: (-item["delta"], item["key"]))
    worsening.sort(key=lambda item: (item["delta"], item["key"]))
    return improving[:2], worsening[:2]


def _format_delta(value: float) -> str:
    return f"{value:+.3f}".replace(".", ",")


def build_macro_score_summary(
    current: MacroScoreSnapshot,
    previous: MacroScoreSnapshot,
) -> MacroScoreSummaryResult:
    current_payload = current.payload
    previous_payload = previous.payload
    current_score = current_payload["score"]
    previous_score = previous_payload["score"]
    if (
        current.origin is not SnapshotOrigin.PRODUCTION_REFRESH
        or previous.origin is not SnapshotOrigin.PRODUCTION_REFRESH
        or current_payload["score_month"] <= previous_payload["score_month"]
        or current_payload["methodology_version"] != previous_payload["methodology_version"]
        or _available_component_keys(current) != _available_component_keys(previous)
        or current_score is None
        or previous_score is None
        or not isfinite(current_score)
        or not isfinite(previous_score)
    ):
        return _UNAVAILABLE

    score_delta = current_score - previous_score
    previous_month = previous_payload["score_month"]
    current_month = current_payload["score_month"]
    if score_delta > 0:
        direction = SummaryDirection.MORE_FAVORABLE
        headline = (
            f"Macro Score ficou {_format_delta(score_delta).lstrip('+')} pontos mais favorável "
            f"entre {previous_month} e {current_month}."
        )
    elif score_delta < 0:
        direction = SummaryDirection.LESS_FAVORABLE
        headline = (
            f"Macro Score ficou {_format_delta(abs(score_delta))} pontos menos favorável "
            f"entre {previous_month} e {current_month}."
        )
    else:
        direction = SummaryDirection.STABLE
        headline = f"Macro Score permaneceu estável entre {previous_month} e {current_month}."
    improving, worsening = _dimension_deltas(current, previous)
    details = [headline]
    if improving:
        details.append("Maiores variações favoráveis: " + ", ".join(
            f"{item['label']} ({_format_delta(item['delta'])} pontos)"
            for item in improving
        ) + ".")
    if worsening:
        details.append("Maiores variações desfavoráveis: " + ", ".join(
            f"{item['label']} ({_format_delta(item['delta'])} pontos)"
            for item in worsening
        ) + ".")
    details.append("Variação observada, sem atribuição causal.")
    payload: MacroScoreSummaryPayload = {
        "template_version": _SUMMARY_TEMPLATE_VERSION,
        "previous_score_month": previous_payload["score_month"],
        "current_score_month": current_payload["score_month"],
        "previous_score": previous_score,
        "current_score": current_score,
        "score_delta": score_delta,
        "direction": direction.value,
        "improving_dimensions": improving,
        "worsening_dimensions": worsening,
        "summary": " ".join(details),
    }
    return MacroScoreSummaryAvailable(payload)
