import os
import sqlite3
from collections.abc import Mapping
from contextlib import closing
from dataclasses import dataclass
from datetime import date, datetime
from math import isfinite
from pathlib import Path
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
    retrospective_di: tuple["RetrospectiveDIObservation", ...] = ()


@dataclass(frozen=True, slots=True)
class RetrospectiveDIObservation:
    trade_date: date
    symbol: str
    maturity: date
    rate: float
    source_hash: str
    source_name: str
    source_member: str
    fetched_at: datetime
    source_basis: str
    maturity_basis: str = "legacy_local_unverified"


def load_retrospective_di_observations(
    *, as_of: datetime,
) -> tuple[RetrospectiveDIObservation, ...]:
    cutoff = utc_datetime(as_of)
    rows = list(store.query_rows(
        "SELECT trade_date,symbol,maturity,rate,source_hash,source_name,source_member,fetched_at,source_basis,maturity_basis "
        "FROM retrospective_di_observations WHERE source_basis = ? AND fetched_at <= ?",
        ("retrospective_reference", cutoff.isoformat()),
    ))
    rows.extend(_configured_di_ledger_rows(cutoff))
    return tuple(
        observation for row in rows
        if (observation := _retrospective_di_observation(row, cutoff)) is not None
    )


def _configured_di_ledger_rows(cutoff: datetime) -> list[dict[str, object]]:
    ledger_path = os.environ.get("XBRAY_RETROSPECTIVE_DI_SQLITE")
    rows: list[dict[str, object]] = []
    if ledger_path:
        path = Path(ledger_path)
        if not path.is_absolute() or path.suffix != ".db":
            raise ValueError("XBRAY_RETROSPECTIVE_DI_SQLITE must be an absolute .db path")
        if not path.exists():
            raise ValueError("configured retrospective DI SQLite ledger does not exist")
        try:
            with closing(sqlite3.connect(f"{path.as_uri()}?mode=ro", uri=True)) as connection:
                connection.row_factory = sqlite3.Row
                columns = {
                    str(row[1]) for row in connection.execute(
                        "PRAGMA table_info(retrospective_di_observations)"
                    ).fetchall()
                }
                maturity_column = ",maturity_basis" if "maturity_basis" in columns else ""
                raw_rows = connection.execute(
                    "SELECT trade_date,symbol,maturity,rate,source_hash,source_name,source_member,fetched_at,source_basis" + maturity_column + " "
                    "FROM retrospective_di_observations WHERE source_basis=? AND fetched_at<=?",
                    ("retrospective_reference", cutoff.isoformat()),
                ).fetchall()
                rows.extend(dict(row) for row in raw_rows)
        except sqlite3.Error as error:
            raise ValueError("could not read configured retrospective DI ledger") from error
    return rows


def _retrospective_di_observation(
    row: Mapping[str, object], cutoff: datetime,
) -> RetrospectiveDIObservation | None:
    try:
        trade_date = date.fromisoformat(str(row["trade_date"]))
        maturity = date.fromisoformat(str(row["maturity"]))
        fetched_at = utc_datetime(
            datetime.fromisoformat(str(row["fetched_at"]).replace("Z", "+00:00"))
        )
        raw_rate = row["rate"]
        if isinstance(raw_rate, bool) or not isinstance(raw_rate, (int, float, str)):
            return None
        rate = float(raw_rate)
    except (KeyError, TypeError, ValueError):
        return None
    required = ("symbol", "source_hash", "source_name", "source_member")
    if (not isfinite(rate) or fetched_at > cutoff or trade_date > cutoff.date()
            or any(not isinstance(row[key], str) or not row[key] for key in required)):
        return None
    return RetrospectiveDIObservation(
        trade_date, str(row["symbol"]), maturity, rate,
        str(row["source_hash"]), str(row["source_name"]),
        str(row["source_member"]), fetched_at, str(row["source_basis"]),
        str(row.get("maturity_basis", "legacy_local_unverified")),
    )


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
        retrospective_di=load_retrospective_di_observations(as_of=cutoff),
    )
