import json
import re
from dataclasses import dataclass
from datetime import date, datetime, timezone
from typing import Final

from pydantic import TypeAdapter

from db import store
from derived.models import SnapshotOrigin, utc_datetime
from macro_score.calculator import MacroScoreResult
from macro_score.serialization import MacroScorePayload, macro_score_payload

_PAYLOAD_ADAPTER: Final = TypeAdapter(MacroScorePayload)
_MONTH_PATTERN: Final = re.compile(r"\d{4}-(0[1-9]|1[0-2])\Z")


@dataclass(frozen=True, slots=True)
class MacroScoreSnapshot:
    payload: MacroScorePayload
    inputs_hash: str
    calculation_cycle_id: str
    origin: SnapshotOrigin
    created_at: datetime


@dataclass(frozen=True, slots=True)
class InvalidMacroScoreSnapshotTimeError(ValueError):
    calculated_at: datetime
    created_at: datetime

    def __str__(self) -> str:
        return "macro score snapshot creation must not precede calculation completion"


@dataclass(frozen=True, slots=True)
class InvalidMacroScoreRangeError(ValueError):
    start: str
    end: str

    def __str__(self) -> str:
        return f"invalid macro score month range: {self.start!r}..{self.end!r}"


@dataclass(frozen=True, slots=True)
class MacroScoreSnapshotIntegrityError(ValueError):
    field: str

    def __str__(self) -> str:
        return f"macro score snapshot column disagrees with payload: {self.field}"


def _month_date(value: str, *, start: str, end: str) -> date:
    if _MONTH_PATTERN.fullmatch(value) is None:
        raise InvalidMacroScoreRangeError(start, end)
    return date.fromisoformat(f"{value}-01")


def _range(start: str, end: str) -> tuple[date, date]:
    first = _month_date(start, start=start, end=end)
    last = _month_date(end, start=start, end=end)
    if first > last:
        raise InvalidMacroScoreRangeError(start, end)
    return first, last


def _canonical_json(payload: MacroScorePayload) -> str:
    return json.dumps(
        payload,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    )


def insert_macro_score_snapshot(
    result: MacroScoreResult,
    *,
    calculation_cycle_id: str,
    origin: SnapshotOrigin,
    created_at: datetime | None = None,
) -> bool:
    created = utc_datetime(created_at or datetime.now(timezone.utc))
    completed = utc_datetime(result.calculated_at)
    if created < completed:
        raise InvalidMacroScoreSnapshotTimeError(completed, created)
    payload = macro_score_payload(
        result,
        calculation_cycle_id=calculation_cycle_id,
        origin=origin,
    )
    changed = store.execute_change(
        "INSERT INTO macro_score_snapshots "
        "(score_month, methodology_version, as_of, calculated_at, inputs_hash, payload_json, "
        "calculation_cycle_id, origin, created_at) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?) "
        "ON CONFLICT (score_month, methodology_version, inputs_hash) DO NOTHING",
        (
            result.score_month.strftime("%Y-%m"),
            result.methodology_version,
            result.as_of.isoformat(),
            completed.isoformat(),
            result.input_fingerprint,
            _canonical_json(payload),
            calculation_cycle_id,
            origin.value,
            created.isoformat(),
        ),
    )
    return changed == 1


def _snapshot_from_row(row: dict[str, object]) -> MacroScoreSnapshot:
    payload = _PAYLOAD_ADAPTER.validate_json(str(row["payload_json"]))
    matching_fields = (
        ("score_month", str(row["score_month"]), payload["score_month"]),
        ("methodology_version", str(row["methodology_version"]), payload["methodology_version"]),
        ("as_of", str(row["as_of"]), payload["as_of"]),
        ("calculated_at", str(row["calculated_at"]), payload["calculated_at"]),
        ("inputs_hash", str(row["inputs_hash"]), payload["inputs_hash"]),
        (
            "calculation_cycle_id",
            str(row["calculation_cycle_id"]),
            payload["calculation_cycle_id"],
        ),
        ("origin", str(row["origin"]), payload["origin"]),
    )
    for field, column_value, payload_value in matching_fields:
        if column_value != payload_value:
            raise MacroScoreSnapshotIntegrityError(field)
    return MacroScoreSnapshot(
        payload=payload,
        inputs_hash=str(row["inputs_hash"]),
        calculation_cycle_id=str(row["calculation_cycle_id"]),
        origin=SnapshotOrigin(str(row["origin"])),
        created_at=utc_datetime(datetime.fromisoformat(str(row["created_at"]))),
    )


def query_macro_score_history(start: str, end: str) -> tuple[MacroScoreSnapshot, ...]:
    first, last = _range(start, end)
    rows = store.query_rows(
        "SELECT score_month, methodology_version, as_of, calculated_at, inputs_hash, payload_json, "
        "calculation_cycle_id, origin, created_at FROM macro_score_snapshots "
        "WHERE origin = ? AND score_month >= ? AND score_month <= ? "
        "ORDER BY score_month ASC, calculated_at DESC, inputs_hash DESC, id DESC",
        (
            SnapshotOrigin.PRODUCTION_REFRESH.value,
            first.strftime("%Y-%m"),
            last.strftime("%Y-%m"),
        ),
    )
    latest_by_month: dict[str, MacroScoreSnapshot] = {}
    for row in rows:
        latest_by_month.setdefault(str(row["score_month"]), _snapshot_from_row(row))
    return tuple(latest_by_month[month] for month in sorted(latest_by_month))


def query_latest_macro_score() -> MacroScoreSnapshot | None:
    rows = store.query_rows(
        "SELECT score_month, methodology_version, as_of, calculated_at, inputs_hash, payload_json, "
        "calculation_cycle_id, origin, created_at FROM macro_score_snapshots "
        "WHERE origin = ? "
        "ORDER BY score_month DESC, calculated_at DESC, inputs_hash DESC, id DESC LIMIT 1",
        (SnapshotOrigin.PRODUCTION_REFRESH.value,),
    )
    return _snapshot_from_row(rows[0]) if rows else None
