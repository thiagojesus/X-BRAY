import hashlib
import json
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import date, datetime, timezone
from math import isfinite
from typing import Final

from pydantic import TypeAdapter

from db import store
from derived.models import (
    DerivedInput,
    DerivedResult,
    DerivedResultPayload,
    SnapshotOrigin,
    result_from_payload,
    result_to_payload,
    utc_datetime,
)
from derived.real_rate import REAL_RATE_KEY, build_real_rate_result

_PAYLOAD_ADAPTER: Final = TypeAdapter(DerivedResultPayload)
_JSON_MAP_ADAPTER: Final = TypeAdapter(dict[str, float | int | str | bool | None])


@dataclass(frozen=True, slots=True)
class DerivedSnapshot:
    result: DerivedResult
    inputs_hash: str
    calculation_cycle_id: str
    origin: SnapshotOrigin
    created_at: datetime


@dataclass(frozen=True, slots=True)
class VintageInputQuery:
    provider: str
    series_key: str
    input_key: str
    unit: str
    value_field: str
    source: str


def _canonical_json(value: Mapping[str, object]) -> str:
    return json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    )


def _inputs_hash(result: DerivedResult) -> str:
    payload = result_to_payload(result, include_dependencies=True)
    inputs = payload["inputs"]
    dependencies = payload.get("dependency_fingerprints")
    identity: Mapping[str, object] = (
        {"inputs": inputs, "dependency_fingerprints": dependencies}
        if dependencies
        else inputs
    )
    return hashlib.sha256(_canonical_json(identity).encode()).hexdigest()


def insert_snapshot(
    result: DerivedResult,
    *,
    calculation_cycle_id: str,
    origin: SnapshotOrigin,
    created_at: datetime | None = None,
) -> bool:
    created = utc_datetime(created_at or datetime.now(timezone.utc))
    payload = _canonical_json(result_to_payload(result, include_dependencies=True))
    changed = store.execute_change(
        "INSERT INTO derived_snapshots "
        "(key, as_of, methodology_version, inputs_hash, payload_json, calculation_cycle_id, origin, created_at) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?) "
        "ON CONFLICT (key, as_of, methodology_version, inputs_hash) DO NOTHING",
        (
            result.key,
            result.as_of.isoformat(),
            result.methodology_version,
            _inputs_hash(result),
            payload,
            calculation_cycle_id,
            origin.value,
            created.isoformat(),
        ),
    )
    return changed == 1


def _snapshot_from_row(row: dict[str, object]) -> DerivedSnapshot:
    payload = _PAYLOAD_ADAPTER.validate_json(str(row["payload_json"]))
    return DerivedSnapshot(
        result=result_from_payload(payload),
        inputs_hash=str(row["inputs_hash"]),
        calculation_cycle_id=str(row["calculation_cycle_id"]),
        origin=SnapshotOrigin(str(row["origin"])),
        created_at=utc_datetime(datetime.fromisoformat(str(row["created_at"]))),
    )


def query_snapshot_history(key: str) -> list[DerivedSnapshot]:
    rows = store.query_rows(
        "SELECT payload_json, inputs_hash, calculation_cycle_id, origin, created_at "
        "FROM derived_snapshots WHERE key = ? ORDER BY as_of, created_at, id",
        (key,),
    )
    return [_snapshot_from_row(row) for row in rows]


def query_snapshot_history_window(
    key: str,
    *,
    start_reference_period: date,
    end_reference_period: date,
    as_of: datetime,
) -> list[DerivedSnapshot]:
    cutoff = utc_datetime(as_of)
    reference_period = (
        "CAST(payload_json AS JSONB) ->> 'reference_date'"
        if store.uses_postgres()
        else "json_extract(payload_json, '$.reference_date')"
    )
    rows = store.query_rows(
        "SELECT payload_json, inputs_hash, calculation_cycle_id, origin, created_at "
        "FROM derived_snapshots WHERE key = ? AND origin = ? AND as_of <= ? AND created_at <= ? "
        f"AND {reference_period} >= ? AND {reference_period} < ? "
        f"ORDER BY {reference_period} DESC, as_of DESC, created_at DESC, inputs_hash DESC, id DESC",
        (
            key,
            SnapshotOrigin.PRODUCTION_REFRESH.value,
            cutoff.isoformat(),
            cutoff.isoformat(),
            start_reference_period.isoformat(),
            end_reference_period.isoformat(),
        ),
    )
    return [_snapshot_from_row(row) for row in rows]


def query_latest_snapshots() -> dict[str, DerivedSnapshot]:
    rows = store.query_rows(
        "SELECT key, payload_json, inputs_hash, calculation_cycle_id, origin, created_at "
        "FROM derived_snapshots ORDER BY key, as_of DESC, created_at DESC, id DESC"
    )
    latest: dict[str, DerivedSnapshot] = {}
    for row in rows:
        latest.setdefault(str(row["key"]), _snapshot_from_row(row))
    return latest


def load_vintage_input(query: VintageInputQuery, *, as_of: datetime) -> DerivedInput | None:
    cutoff = utc_datetime(as_of)
    rows = store.query_rows(
        "SELECT reference_period, raw_value_json, available_at FROM observation_vintages "
        "WHERE provider = ? AND series_key = ? AND available_at <= ? "
        "ORDER BY reference_period DESC, available_at DESC, first_seen_at DESC, payload_hash DESC LIMIT 1",
        (query.provider, query.series_key, cutoff.isoformat()),
    )
    if not rows:
        return None
    row = rows[0]
    raw = _JSON_MAP_ADAPTER.validate_json(str(row["raw_value_json"]))
    value = raw.get(query.value_field)
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    parsed = float(value)
    if not isfinite(parsed):
        return None
    return DerivedInput(
        key=query.input_key,
        value=parsed,
        unit=query.unit,
        reference_period=str(row["reference_period"]),
        available_at=datetime.fromisoformat(str(row["available_at"])),
        source=query.source,
    )


def refresh_derived_snapshots(as_of: datetime, calculation_cycle_id: str) -> int:
    history = tuple(
        snapshot.result
        for snapshot in query_snapshot_history(REAL_RATE_KEY)
    )
    result = build_real_rate_result(as_of=as_of, history=history)
    return int(insert_snapshot(
        result,
        calculation_cycle_id=calculation_cycle_id,
        origin=SnapshotOrigin.PRODUCTION_REFRESH,
    ))
