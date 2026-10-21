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
    di_instrument_snapshots: tuple["DIInstrumentSnapshot", ...] = ()


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
    security_id: str | None = None
    security_identity_conflict: bool = False


@dataclass(frozen=True, slots=True)
class DIInstrumentSnapshot:
    report_date: date
    symbol: str
    security_id: str
    fin_instrm_id: str
    maturity: date
    trading_start: date | None
    trading_end: date | None
    source_hash: str
    source_name: str
    message_definition: str
    message_id: str
    captured_at: datetime
    available_at: datetime
    source_basis: str


def load_retrospective_di_observations(
    *, as_of: datetime,
) -> tuple[RetrospectiveDIObservation, ...]:
    cutoff = utc_datetime(as_of)
    rows = list(store.query_rows(
        "WITH price_identity_summary AS ("
        "SELECT trade_date,symbol,source_hash,COUNT(*) AS identity_count,"
        "COUNT(security_id) AS nonnull_identity_count,"
        "COUNT(DISTINCT security_id) AS distinct_identity_count,MIN(security_id) AS security_id "
        "FROM retrospective_di_price_identities WHERE source_basis=? "
        "GROUP BY trade_date,symbol,source_hash) "
        "SELECT o.trade_date,o.symbol,o.maturity,o.rate,o.source_hash,o.source_name,"
        "o.source_member,o.fetched_at,o.source_basis,o.maturity_basis,"
        "CASE WHEN i.identity_count IS NULL THEN o.security_id "
        "WHEN i.nonnull_identity_count=i.identity_count AND i.distinct_identity_count=1 "
        "AND (o.security_id IS NULL OR o.security_id=i.security_id) THEN i.security_id "
        "ELSE NULL END AS security_id,"
        "CASE WHEN i.identity_count IS NOT NULL AND ("
        "i.nonnull_identity_count<>i.identity_count OR i.distinct_identity_count<>1 "
        "OR (o.security_id IS NOT NULL AND o.security_id<>i.security_id)) "
        "THEN TRUE ELSE FALSE END AS security_identity_conflict "
        "FROM retrospective_di_observations o LEFT JOIN price_identity_summary i "
        "ON i.trade_date=o.trade_date AND i.symbol=o.symbol AND i.source_hash=o.source_hash "
        "WHERE o.source_basis=? AND o.fetched_at <= ?",
        ("b3_price_identity_v1", "retrospective_reference", cutoff.isoformat()),
    ))
    rows.extend(_configured_di_ledger_rows(cutoff))
    return tuple(
        observation for row in rows
        if (observation := _retrospective_di_observation(row, cutoff)) is not None
    )


def load_di_instrument_snapshots(*, as_of: datetime) -> tuple[DIInstrumentSnapshot, ...]:
    cutoff = utc_datetime(as_of)
    try:
        ledger = os.environ.get("XBRAY_RETROSPECTIVE_DI_SQLITE")
        if ledger:
            path = Path(ledger)
            if not path.is_absolute() or path.suffix != ".db" or not path.exists():
                raise ValueError("configured retrospective DI SQLite ledger is invalid")
            with closing(sqlite3.connect(f"{path.as_uri()}?mode=ro", uri=True)) as connection:
                connection.row_factory = sqlite3.Row
                rows = connection.execute(
                    "SELECT report_date,symbol,security_id,fin_instrm_id,maturity,trading_start,trading_end,source_hash,"
                    "source_name,message_definition,message_id,captured_at,available_at,source_basis "
                    "FROM b3_di_instrument_snapshots WHERE available_at <= ?",
                    (cutoff.isoformat(),),
                ).fetchall()
        else:
            rows = store.query_rows(
                "SELECT report_date,symbol,security_id,fin_instrm_id,maturity,trading_start,trading_end,source_hash,"
                "source_name,message_definition,message_id,captured_at,available_at,source_basis "
                "FROM b3_di_instrument_snapshots WHERE available_at <= ?",
                (cutoff.isoformat(),),
            )
    except sqlite3.Error as error:
        # The table is introduced by the additive migration; old test databases may lack it.
        if "no such table" in str(error).lower() or "does not exist" in str(error).lower():
            return ()
        raise
    result: list[DIInstrumentSnapshot] = []
    for row in rows:
        try:
            if row["source_basis"] != "b3_instrument_snapshot_v1" or row["message_definition"] != "bvmf.100.02":
                continue
            result.append(DIInstrumentSnapshot(
                date.fromisoformat(str(row["report_date"])), str(row["symbol"]),
                str(row["security_id"]), str(row["fin_instrm_id"]), date.fromisoformat(str(row["maturity"])),
                date.fromisoformat(str(row["trading_start"])) if row["trading_start"] else None,
                date.fromisoformat(str(row["trading_end"])) if row["trading_end"] else None,
                str(row["source_hash"]), str(row["source_name"]), str(row["message_definition"]),
                str(row["message_id"]), utc_datetime(datetime.fromisoformat(str(row["captured_at"]))),
                utc_datetime(datetime.fromisoformat(str(row["available_at"]))), str(row["source_basis"]),
            ))
        except (KeyError, TypeError, ValueError):
            continue
    return tuple(result)


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
                security_column = ",security_id" if "security_id" in columns else ""
                raw_rows = connection.execute(
                    "SELECT trade_date,symbol,maturity,rate,source_hash,source_name,source_member,fetched_at,source_basis" + maturity_column + security_column + " "
                    "FROM retrospective_di_observations WHERE source_basis=? AND fetched_at<=?",
                    ("retrospective_reference", cutoff.isoformat()),
                ).fetchall()
                try:
                    identity_rows = connection.execute(
                        "SELECT trade_date,symbol,source_hash,security_id "
                        "FROM retrospective_di_price_identities "
                        "WHERE source_basis='b3_price_identity_v1'"
                    ).fetchall()
                except sqlite3.Error as error:
                    if "no such table" not in str(error).lower():
                        raise
                    identity_rows = []
                identity_candidates: dict[tuple[str, str, str], set[str]] = {}
                identity_counts: dict[tuple[str, str, str], tuple[int, int]] = {}
                for identity_row in identity_rows:
                    key = (str(identity_row[0]), str(identity_row[1]), str(identity_row[2]))
                    identity_candidates.setdefault(key, set())
                    total, nonnull = identity_counts.get(key, (0, 0))
                    identity_counts[key] = (total + 1, nonnull + (identity_row[3] is not None))
                    if identity_row[3] is not None:
                        identity_candidates[key].add(str(identity_row[3]))
                for raw in raw_rows:
                    item = dict(raw)
                    key = (
                        str(item["trade_date"]), str(item["symbol"]),
                        str(item["source_hash"]),
                    )
                    total, nonnull = identity_counts.get(key, (0, 0))
                    candidates = identity_candidates.get(key, set())
                    existing = item.get("security_id")
                    conflict = total > 0 and (
                        nonnull != total or len(candidates) != 1
                        or (existing is not None and existing != next(iter(candidates)))
                    )
                    item["security_identity_conflict"] = conflict
                    if total == 0:
                        item["security_id"] = existing
                    elif not conflict:
                        item["security_id"] = next(iter(candidates))
                    else:
                        item["security_id"] = None
                    rows.append(item)
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
        str(row["security_id"]) if row.get("security_id") else None,
        row.get("security_identity_conflict") is True
        or str(row.get("security_identity_conflict", "")).lower() in {"1", "true"},
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


def load_score_vintages(
    *, as_of: datetime, database: Path | None = None,
) -> tuple[ScoreVintage, ...]:
    cutoff = utc_datetime(as_of)
    keys = tuple(sorted(_SGS_SPECS))
    placeholders = ", ".join("?" for _key in keys)
    query = (
        "SELECT provider, series_key, reference_period, raw_value_json, source_published_at, "
        "first_seen_at, available_at, payload_hash FROM observation_vintages "
        f"WHERE available_at <= ? AND ((provider = ? AND series_key IN ({placeholders})) "
        "OR (provider = ? AND series_key = ?)) "
        "ORDER BY provider, series_key, reference_period, available_at, first_seen_at, payload_hash"
    )
    params = (cutoff.isoformat(), _SGS_PROVIDER, *keys, _FOCUS_PROVIDER, FOCUS_12M_KEY)
    if database is None:
        rows = store.query_rows(query, params)
    else:
        path = Path(database)
        if not path.is_absolute() or path.suffix != ".db" or not path.exists():
            raise ValueError("vintage source must be an existing absolute SQLite .db")
        try:
            with closing(sqlite3.connect(f"{path.as_uri()}?mode=ro", uri=True)) as connection:
                connection.row_factory = sqlite3.Row
                rows = connection.execute(query, params).fetchall()
        except sqlite3.Error as error:
            raise ValueError("could not read score vintages from source SQLite") from error
    vintages: list[ScoreVintage] = []
    for raw_row in rows:
        try:
            row = _VintageRow.model_validate(dict(raw_row))
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
