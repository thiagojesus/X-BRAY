from collections.abc import Mapping
from dataclasses import dataclass
from datetime import date, datetime
from types import MappingProxyType
from typing import Final, Literal, assert_never

from pydantic import BaseModel, ConfigDict, Field, StrictInt, ValidationError

from config import BCB_ODATA_BASE
from datafetchers.focus import FOCUS_12M_KEY
from db import store
from derived.inflation_target_inputs import (
    InflationTargetObservation,
    load_inflation_target_observations,
)
from derived.models import utc_datetime
from derived.repository import DerivedSnapshot, query_snapshot_history
from indicator_catalog import (
    INDICATOR_CATALOG,
    IndicatorKey,
    IndicatorSpec,
    Transformation,
)
from macro_score.inputs import ScoreVintage
from macro_score.methodology import COMPONENTS

_SGS_PROVIDER: Final = "bcb_sgs"
_FOCUS_PROVIDER: Final = "focus"
_FOCUS_SOURCE: Final = f"{BCB_ODATA_BASE}/ExpectativasMercadoInflacao12Meses"
_DERIVED_SOURCE_KEYS: Final = (
    "juro_real_ex_ante_1y",
    "inclinacao_di_5y_1y",
    "spread_credito_pf_selic",
    "cobertura_externa",
)


@dataclass(frozen=True, slots=True)
class MacroScoreSources:
    vintages: tuple[ScoreVintage, ...]
    target_observations: tuple[InflationTargetObservation, ...]
    derived_snapshots: tuple[DerivedSnapshot, ...]


class _VintageRow(BaseModel):
    model_config = ConfigDict(frozen=True, extra="ignore")

    provider: str
    series_key: str
    reference_period: date
    raw_value_json: str
    source_published_at: datetime | None
    first_seen_at: datetime
    available_at: datetime
    payload_hash: str


class _SgsValue(BaseModel):
    model_config = ConfigDict(frozen=True, extra="ignore")

    valor: float = Field(allow_inf_nan=False)


class _FocusValue(BaseModel):
    model_config = ConfigDict(frozen=True, extra="ignore")

    Indicador: Literal["IPCA"]
    Data: str
    Mediana: float = Field(allow_inf_nan=False)
    Suavizada: Literal["S"]
    baseCalculo: StrictInt


class UnsupportedMacroScoreTransformationError(Exception):
    series_key: str
    transformation: Transformation

    def __init__(self, series_key: str, transformation: Transformation) -> None:
        self.series_key = series_key
        self.transformation = transformation
        super().__init__(str(self))

    def __str__(self) -> str:
        return f"unsupported score transformation for {self.series_key}: {self.transformation.value}"


_SGS_SOURCE_KEYS: Final = frozenset(
    source_key
    for component in COMPONENTS
    for source_key in component.source_keys
    if IndicatorKey(source_key) in INDICATOR_CATALOG
)
_SGS_SPECS: Final[Mapping[str, IndicatorSpec]] = MappingProxyType({
    str(key): spec
    for key, spec in INDICATOR_CATALOG.items()
    if str(key) in _SGS_SOURCE_KEYS
})


def _transformed_value(value: float, spec: IndicatorSpec) -> float:
    match spec.transformation:
        case None:
            return value
        case Transformation.INVERT_SIGN:
            return -value
        case Transformation.ANNUALIZE_12 | Transformation.ANNUALIZE_252:
            raise UnsupportedMacroScoreTransformationError(
                str(spec.key),
                spec.transformation,
            )
    assert_never(spec.transformation)


def _sgs_vintage(row: _VintageRow) -> ScoreVintage | None:
    spec = _SGS_SPECS.get(row.series_key)
    if spec is None:
        return None
    try:
        raw = _SgsValue.model_validate_json(row.raw_value_json)
    except ValidationError:
        return None
    return ScoreVintage(
        provider=row.provider,
        series_key=row.series_key,
        reference_period=row.reference_period,
        value=_transformed_value(raw.valor, spec),
        unit=spec.unit.value,
        source=spec.source_url,
        available_at=row.available_at,
        first_seen_at=row.first_seen_at,
        source_published_at=row.source_published_at,
        payload_hash=row.payload_hash,
    )


def _focus_vintage(row: _VintageRow) -> ScoreVintage | None:
    try:
        record = _FocusValue.model_validate_json(row.raw_value_json)
        if record.baseCalculo != 0 or record.Data != row.reference_period.isoformat():
            return None
        reference_period = row.reference_period
    except (ValidationError, ValueError):
        return None
    return ScoreVintage(
        provider=row.provider,
        series_key=FOCUS_12M_KEY,
        reference_period=reference_period,
        value=record.Mediana,
        unit="pct_annual",
        source=_FOCUS_SOURCE,
        available_at=row.available_at,
        first_seen_at=row.first_seen_at,
        source_published_at=row.source_published_at,
        payload_hash=row.payload_hash,
    )


def load_score_vintages(*, as_of: datetime) -> tuple[ScoreVintage, ...]:
    cutoff = utc_datetime(as_of)
    keys = tuple(sorted(_SGS_SPECS))
    placeholders = ", ".join("?" for _key in keys)
    rows = store.query_rows(
        "SELECT provider, series_key, reference_period, raw_value_json, source_published_at, "
        "first_seen_at, available_at, payload_hash FROM observation_vintages "
        f"WHERE available_at <= ? AND ((provider = ? AND series_key IN ({placeholders})) "
        "OR (provider = ? AND series_key = ?)) "
        "ORDER BY provider, series_key, reference_period, available_at, first_seen_at, payload_hash",
        (cutoff.isoformat(), _SGS_PROVIDER, *keys, _FOCUS_PROVIDER, FOCUS_12M_KEY),
    )
    vintages: list[ScoreVintage] = []
    for raw_row in rows:
        try:
            row = _VintageRow.model_validate(raw_row)
        except ValidationError:
            continue
        vintage = (
            _sgs_vintage(row)
            if row.provider == _SGS_PROVIDER
            else _focus_vintage(row)
        )
        if vintage is not None:
            vintages.append(vintage)
    return tuple(vintages)


def load_macro_score_sources(*, as_of: datetime) -> MacroScoreSources:
    cutoff = utc_datetime(as_of)
    snapshots = tuple(
        snapshot
        for key in _DERIVED_SOURCE_KEYS
        for snapshot in query_snapshot_history(key)
    )
    return MacroScoreSources(
        vintages=load_score_vintages(as_of=cutoff),
        target_observations=load_inflation_target_observations(as_of=cutoff),
        derived_snapshots=snapshots,
    )
