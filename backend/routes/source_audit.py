"""Authenticated, read-only snapshot endpoint for the weekly source auditor."""

import json
from datetime import datetime, timezone
from typing import cast

from fastapi import APIRouter, Depends

from db.store import source_audit_rows
from refresh_auth import require_refresh_auth
from source_audit.catalog import inventory

router = APIRouter(prefix="/api/source-audit", tags=["source-audit"])


def _decode_json_value(value: object) -> object | None:
    if isinstance(value, (dict, list)):
        return cast(object, value)
    if not isinstance(value, str):
        return None
    try:
        return cast(object, json.loads(value))
    except (TypeError, ValueError):
        return None


def _decode_rows(rows: dict[str, list[dict[str, object]]]) -> dict[str, list[dict[str, object]]]:
    decoded: dict[str, list[dict[str, object]]] = {}
    for table, records in rows.items():
        decoded[table] = []
        for record in records:
            item = dict(record)
            if table in {"focus", "anbima", "treasury"}:
                parsed = _decode_json_value(item.get("data_json"))
                if parsed is None:
                    item["data_json"] = None
                    item["decode_error"] = True
                else:
                    item["data_json"] = parsed
            if table == "focus_vintages":
                parsed = _decode_json_value(item.get("raw_value_json"))
                if parsed is None:
                    item["raw_value_json"] = None
                    item["decode_error"] = True
                else:
                    item["raw_value_json"] = parsed
            decoded[table].append(item)
    return decoded


@router.get("/snapshot", dependencies=[Depends(require_refresh_auth)])
def source_audit_snapshot() -> dict[str, object]:
    """Return a bounded current-data snapshot; this endpoint never mutates storage."""
    rows = source_audit_rows()
    return {
        "schema_version": "source-audit-snapshot-v1",
        "captured_at": datetime.now(timezone.utc).isoformat(),
        "inventory": inventory(),
        "database": _decode_rows(rows),
    }
