import hashlib
import json
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import date, datetime, timezone
from typing import Final, Literal, TypeAlias, TypedDict

from pydantic import BaseModel, ConfigDict, Field

from db import store
from derived.inflation_targets import (
    INFLATION_TARGET_RULES,
    InflationTargetRule,
)
from derived.models import utc_datetime

_PROVIDER: Final = "bcb_cmn"
_SERIES_KEY: Final = "inflation_target_rules"
DatabaseValue: TypeAlias = str | int | float | bool | None


class _RulePayload(TypedDict):
    rule_id: str
    table_version: str
    regime: str
    effective_from: str
    effective_to: str | None
    center_pct: float
    tolerance_band_pp: float
    source_url: str
    legal_reference: str | None
    direct_source_url: str | None


class _RuleDimensions(TypedDict):
    rule_id: str
    table_version: str


class _RulePayloadModel(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    rule_id: str = Field(min_length=1)
    table_version: str = Field(min_length=1)
    regime: str = Field(min_length=1)
    effective_from: date
    effective_to: date | None
    center_pct: float = Field(allow_inf_nan=False)
    tolerance_band_pp: float = Field(ge=0, allow_inf_nan=False)
    source_url: str = Field(min_length=1)
    legal_reference: str | None
    direct_source_url: str | None


class _RuleDimensionsModel(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    rule_id: str = Field(min_length=1)
    table_version: str = Field(min_length=1)


class _VintageRow(BaseModel):
    model_config = ConfigDict(frozen=True, extra="ignore")

    reference_period: str
    dimensions_json: str
    dimensions_hash: str
    raw_value_json: str
    source_published_at: str | None
    first_seen_at: datetime
    available_at: datetime
    payload_hash: str


@dataclass(frozen=True, slots=True)
class InflationTargetObservation:
    rule: InflationTargetRule
    first_seen_at: datetime
    payload_hash: str

    @property
    def available_at(self) -> datetime:
        return self.first_seen_at


@dataclass(frozen=True, slots=True)
class InflationTargetLedgerError(ValueError):
    issue: Literal[
        "rule_identity",
        "availability",
        "payload_hash",
        "ambiguous_revision",
        "ambiguous_rule",
    ]

    def __str__(self) -> str:
        return f"invalid inflation-target ledger row: {self.issue}"


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _rule_payload(rule: InflationTargetRule) -> _RulePayload:
    return {
        "rule_id": rule.rule_id,
        "table_version": rule.table_version,
        "regime": rule.regime,
        "effective_from": rule.effective_from.isoformat(),
        "effective_to": rule.effective_to.isoformat() if rule.effective_to else None,
        "center_pct": rule.center_pct,
        "tolerance_band_pp": rule.tolerance_band_pp,
        "source_url": rule.source_url,
        "legal_reference": rule.legal_reference,
        "direct_source_url": rule.direct_source_url,
    }


def _canonical_json(value: _RulePayload | _RuleDimensions) -> str:
    return json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    )


def _vintage_row(
    rule: InflationTargetRule,
    first_seen_at: datetime,
) -> tuple[DatabaseValue, ...]:
    payload = _rule_payload(rule)
    payload_json = _canonical_json(payload)
    dimensions: _RuleDimensions = {"rule_id": rule.rule_id, "table_version": rule.table_version}
    dimensions_json = _canonical_json(dimensions)
    dimensions_hash = hashlib.sha256(dimensions_json.encode("utf-8")).hexdigest()
    payload_hash = hashlib.sha256(payload_json.encode("utf-8")).hexdigest()
    observed = utc_datetime(first_seen_at).isoformat()
    return (
        _PROVIDER,
        _SERIES_KEY,
        rule.effective_from.isoformat(),
        dimensions_json,
        dimensions_hash,
        payload_json,
        None,
        observed,
        observed,
        payload_hash,
        payload_hash[:16],
    )


def _insert_statement(*, postgres: bool) -> str:
    placeholder = "%s" if postgres else "?"
    values = ", ".join((placeholder,) * 11)
    return (
        "INSERT INTO observation_vintages "
        "(provider, series_key, reference_period, dimensions_json, dimensions_hash, raw_value_json, "
        "source_published_at, first_seen_at, available_at, payload_hash, revision_id) "
        f"VALUES ({values}) "
        "ON CONFLICT (provider, series_key, reference_period, dimensions_hash, payload_hash) DO NOTHING"
    )


def observe_inflation_target_rules(
    rules: Sequence[InflationTargetRule] = INFLATION_TARGET_RULES,
) -> int:
    """Persist complete current rule revisions using the local first-observation time."""
    observed_at = utc_datetime(_now())
    rows: list[tuple[DatabaseValue, ...]] = [_vintage_row(rule, observed_at) for rule in rules]
    if rows:
        store.execute_many(_insert_statement(postgres=store.uses_postgres()), rows)
    return len(rows)


def _rule_from_payload(raw_value_json: str) -> InflationTargetRule:
    payload = _RulePayloadModel.model_validate_json(raw_value_json)
    return InflationTargetRule(
        rule_id=payload.rule_id,
        table_version=payload.table_version,
        regime=payload.regime,
        effective_from=payload.effective_from,
        effective_to=payload.effective_to,
        center_pct=payload.center_pct,
        tolerance_band_pp=payload.tolerance_band_pp,
        source_url=payload.source_url,
        legal_reference=payload.legal_reference,
        direct_source_url=payload.direct_source_url,
    )


def _observation(row: _VintageRow) -> InflationTargetObservation:
    dimensions = _RuleDimensionsModel.model_validate_json(row.dimensions_json)
    rule = _rule_from_payload(row.raw_value_json)
    expected_dimensions = (rule.rule_id, rule.table_version)
    expected_dimensions_hash = hashlib.sha256(row.dimensions_json.encode("utf-8")).hexdigest()
    if (
        (dimensions.rule_id, dimensions.table_version) != expected_dimensions
        or expected_dimensions_hash != row.dimensions_hash
    ):
        raise InflationTargetLedgerError("rule_identity")
    if row.reference_period != rule.effective_from.isoformat():
        raise InflationTargetLedgerError("rule_identity")

    first_seen_at = utc_datetime(row.first_seen_at)
    available_at = utc_datetime(row.available_at)
    if row.source_published_at is not None or first_seen_at != available_at:
        raise InflationTargetLedgerError("availability")

    payload_hash = hashlib.sha256(_canonical_json(_rule_payload(rule)).encode("utf-8")).hexdigest()
    if payload_hash != row.payload_hash:
        raise InflationTargetLedgerError("payload_hash")
    return InflationTargetObservation(rule, first_seen_at, payload_hash)


def load_inflation_target_observations(
    *,
    as_of: datetime,
) -> tuple[InflationTargetObservation, ...]:
    """Load persisted target rule revisions known at the UTC calculation cutoff."""
    cutoff = utc_datetime(as_of)
    rows = store.query_rows(
        "SELECT reference_period, dimensions_json, dimensions_hash, raw_value_json, "
        "source_published_at, first_seen_at, available_at, payload_hash "
        "FROM observation_vintages WHERE provider = ? AND series_key = ? AND available_at <= ? "
        "ORDER BY reference_period, available_at, first_seen_at, payload_hash",
        (_PROVIDER, _SERIES_KEY, cutoff.isoformat()),
    )
    return tuple(
        _observation(_VintageRow.model_validate(row))
        for row in rows
    )
