import hashlib
import json
from collections.abc import Mapping, Sequence
from datetime import datetime, timezone
from math import isfinite
from typing import TypeAlias

from db import store

JsonValue: TypeAlias = str | int | float | bool | None | list["JsonValue"] | dict[str, "JsonValue"]


class SidraObservationError(ValueError):
    pass

_FIELDS = (
    "dataset_key",
    "table_id",
    "variable_id",
    "classification_id",
    "category_id",
    "territory_level",
    "territory_code",
    "reference_period",
    "reference_label",
    "value",
    "unit",
    "base_version",
    "source_published_at",
)


def _utc_timestamp(value: object | None = None) -> str:
    if value is None:
        return datetime.now(timezone.utc).isoformat()
    if not isinstance(value, str):
        raise SidraObservationError("SIDRA timestamp must be an ISO string")
    parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if parsed.tzinfo is None:
        raise SidraObservationError("SIDRA timestamp must include a timezone")
    return parsed.astimezone(timezone.utc).isoformat()


def _required_text(observation: Mapping[str, object], field: str) -> str:
    value = observation.get(field)
    if not isinstance(value, str) or not value.strip():
        raise SidraObservationError(f"invalid SIDRA {field}")
    return value


def _required_int(observation: Mapping[str, object], field: str) -> int:
    value = observation.get(field)
    if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
        raise SidraObservationError(f"invalid SIDRA {field}")
    return value


def _optional_int(observation: Mapping[str, object], field: str) -> int:
    value = observation.get(field)
    if value is None:
        return 0
    if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
        raise SidraObservationError(f"invalid SIDRA {field}")
    return value


def _optional_text(observation: Mapping[str, object], field: str) -> str | None:
    value = observation.get(field)
    if value is None:
        return None
    if not isinstance(value, str) or not value.strip():
        raise SidraObservationError(f"invalid SIDRA {field}")
    return value


def _normalized_row(
    observation: Mapping[str, object],
    first_seen_at: str,
) -> tuple[object, ...]:
    reference_period = _required_text(observation, "reference_period")
    if len(reference_period) != 6 or not reference_period.isdigit():
        raise SidraObservationError("invalid SIDRA reference_period")
    raw_value = observation.get("value")
    if isinstance(raw_value, bool) or not isinstance(raw_value, (int, float)):
        raise SidraObservationError("invalid SIDRA value")
    value = float(raw_value)
    if not isfinite(value):
        raise SidraObservationError("invalid SIDRA value")
    source_published_at = _optional_text(observation, "source_published_at")
    published_at = _utc_timestamp(source_published_at) if source_published_at else None
    canonical: dict[str, JsonValue] = {
        "dataset_key": _required_text(observation, "dataset_key"),
        "table_id": _required_int(observation, "table_id"),
        "variable_id": _required_int(observation, "variable_id"),
        "classification_id": _optional_int(observation, "classification_id"),
        "category_id": _optional_int(observation, "category_id"),
        "territory_level": _required_text(observation, "territory_level"),
        "territory_code": _required_text(observation, "territory_code"),
        "reference_period": reference_period,
        "reference_label": _required_text(observation, "reference_label"),
        "value": value,
        "unit": _required_text(observation, "unit"),
        "base_version": _optional_text(observation, "base_version"),
        "source_published_at": published_at,
    }
    payload = json.dumps(
        canonical,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    )
    return (
        _required_text(observation, "dataset_key"),
        _required_int(observation, "table_id"),
        _required_int(observation, "variable_id"),
        _optional_int(observation, "classification_id"),
        _optional_int(observation, "category_id"),
        _required_text(observation, "territory_level"),
        _required_text(observation, "territory_code"),
        reference_period,
        _required_text(observation, "reference_label"),
        value,
        _required_text(observation, "unit"),
        _optional_text(observation, "base_version"),
        published_at,
        first_seen_at,
        published_at or first_seen_at,
        hashlib.sha256(payload.encode("utf-8")).hexdigest(),
    )


def insert_sidra_observations(
    observations: Sequence[Mapping[str, object]],
    *,
    first_seen_at: str | None = None,
) -> None:
    seen_at = _utc_timestamp(first_seen_at)
    rows = [_normalized_row(observation, seen_at) for observation in observations]
    if not rows:
        return
    columns = (
        "dataset_key, table_id, variable_id, classification_id, category_id, "
        "territory_level, territory_code, reference_period, reference_label, value, "
        "unit, base_version, source_published_at, first_seen_at, available_at, payload_hash"
    )
    if store.uses_postgres():
        sql = (
            f"INSERT INTO sidra_observations ({columns}) VALUES ({', '.join(['%s'] * 16)}) "
            "ON CONFLICT (dataset_key, table_id, variable_id, classification_id, category_id, "
            "territory_level, territory_code, reference_period, payload_hash) DO NOTHING"
        )
        store.execute_many(sql, rows)
        return
    sql = f"INSERT OR IGNORE INTO sidra_observations ({columns}) VALUES ({', '.join(['?'] * 16)})"
    store.execute_many(sql, rows)


def _public_row(row: dict[str, object]) -> dict[str, object]:
    return {
        **row,
        "classification_id": row["classification_id"] or None,
        "category_id": row["category_id"] or None,
    }


def query_sidra_history(dataset_key: str) -> list[dict[str, object]]:
    rows = store.query_rows(
        "SELECT dataset_key, table_id, variable_id, classification_id, category_id, "
        "territory_level, territory_code, reference_period, reference_label, value, unit, "
        "base_version, source_published_at, first_seen_at, available_at, payload_hash "
        "FROM sidra_observations WHERE dataset_key = ? "
        "ORDER BY reference_period, available_at, first_seen_at, payload_hash",
        (dataset_key,),
    )
    return [_public_row(row) for row in rows]


def query_sidra_current(
    dataset_keys: Sequence[str],
    *,
    as_of: str | None = None,
) -> dict[str, list[dict[str, object]]]:
    if not dataset_keys:
        return {}
    placeholders = ", ".join("?" for _key in dataset_keys)
    sql = (
        "SELECT dataset_key, table_id, variable_id, classification_id, category_id, "
        "territory_level, territory_code, reference_period, reference_label, value, unit, "
        "base_version, source_published_at, first_seen_at, available_at, payload_hash "
        f"FROM sidra_observations WHERE dataset_key IN ({placeholders})"
    )
    params: list[object] = list(dataset_keys)
    if as_of is not None:
        sql += " AND available_at <= ?"
        params.append(_utc_timestamp(as_of))
    sql += " ORDER BY dataset_key, reference_period, available_at DESC, first_seen_at DESC, payload_hash DESC"
    rows = store.query_rows(sql, params)
    current: dict[tuple[object, ...], dict[str, object]] = {}
    for row in rows:
        identity = tuple(row[field] for field in (
            "dataset_key",
            "table_id",
            "variable_id",
            "classification_id",
            "category_id",
            "territory_level",
            "territory_code",
            "reference_period",
        ))
        current.setdefault(identity, _public_row(row))
    grouped: dict[str, list[dict[str, object]]] = {}
    for row in current.values():
        grouped.setdefault(str(row["dataset_key"]), []).append(row)
    for values in grouped.values():
        values.sort(key=lambda row: str(row["reference_period"]))
    return grouped
