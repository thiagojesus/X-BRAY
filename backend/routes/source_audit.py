"""Authenticated, read-only snapshot endpoint for the weekly source auditor."""

import hashlib
import json
import os
import re
from datetime import datetime, timezone
from threading import Lock
from time import monotonic
from typing import Literal, cast

from fastapi import APIRouter, Depends, HTTPException, Query, Request, Response

from db.store import source_audit_rows
from refresh_auth import require_refresh_auth
from source_audit.auditor import collect_provider_probe
from source_audit.catalog import inventory

router = APIRouter(prefix="/api/source-audit", tags=["source-audit"])
_PUBLIC_AUDIT_HOSTS = frozenset({"xbray-api.vercel.app"})
_PROBE_LOCK = Lock()
_last_b3_probe_started: float | None = None


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
            if table == "source_extractions":
                parsed = _decode_json_value(item.get("qualifiers_json"))
                item["qualifiers"] = parsed if isinstance(parsed, dict) else None
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


def _focus_fingerprint(rows: list[dict[str, object]], payload_key: str, time_key: str) -> dict[str, object]:
    identities: list[str] = []
    survey_dates: list[str] = []
    available_times: list[str] = []
    for row in rows:
        payload = _source_object(row.get(payload_key))
        if payload is None:
            continue
        indicator = str(payload.get("Indicador") or row.get("indicator") or row.get("series_key") or "")
        target = str(payload.get("DataReferencia") or "")
        base = str(payload.get("baseCalculo") if payload.get("baseCalculo") is not None else "")
        smooth = str(payload.get("Suavizada") or "")
        survey = str(payload.get("Data") or "")
        identities.append(f"{indicator}|{target}|{base}|{smooth}|{survey}")
        if survey:
            survey_dates.append(survey[:10])
        available = row.get(time_key)
        if isinstance(available, str) and available:
            available_times.append(available)
    digest = hashlib.sha256("\n".join(sorted(identities)).encode("utf-8")).hexdigest()
    return {
        "row_count": len(rows),
        "identity_reference_sha256": digest,
        "latest_survey_date": max(survey_dates, default=None),
        "latest_available_at": max(available_times, default=None),
    }


def _snapshot_provenance(rows: dict[str, list[dict[str, object]]]) -> dict[str, object]:
    return {
        "focus_current": _focus_fingerprint(rows.get("focus", []), "data_json", "fetched_at"),
        "focus_vintages": _focus_fingerprint(rows.get("focus_vintages", []), "raw_value_json", "available_at"),
    }


def _runtime_provenance() -> dict[str, str | None]:
    commit = os.environ.get("VERCEL_GIT_COMMIT_SHA", "")
    environment = os.environ.get("VERCEL_ENV", "")
    return {
        "commit_sha": commit.lower() if re.fullmatch(r"[0-9a-fA-F]{7,40}", commit) else None,
        "environment": environment if environment in {"production", "preview", "development"} else None,
    }


@router.get("/snapshot", dependencies=[Depends(require_refresh_auth)])
def source_audit_snapshot(request: Request) -> dict[str, object]:
    """Return a bounded current-data snapshot; this endpoint never mutates storage."""
    rows = source_audit_rows()
    host = request.url.hostname.lower().rstrip(".") if request.url.hostname else ""
    decoded = _decode_rows(rows)
    limits = decoded.get("audit_limits", [])
    database_snapshot_at = limits[0].get("database_snapshot_at") if limits else None
    return {
        "schema_version": "source-audit-snapshot-v1",
        "captured_at": datetime.now(timezone.utc).isoformat(),
        "database_snapshot_at": database_snapshot_at,
        "runtime": _runtime_provenance(),
        "request_provenance": {
            "request_host": host if host in _PUBLIC_AUDIT_HOSTS else None,
            "matches_expected_production_host": host == "xbray-api.vercel.app",
        },
        "inventory": inventory(),
        "database_provenance": _snapshot_provenance(decoded),
        "database": decoded,
    }


@router.get("/probe", dependencies=[Depends(require_refresh_auth)])
def source_audit_provider_probe(
    response: Response,
    provider: Literal["b3", "tesouro"] = Query(),
    checked_at: str = Query(min_length=20, max_length=40),
) -> dict[str, object]:
    """Fetch one allowlisted official source through the independent audit parser."""
    global _last_b3_probe_started
    response.headers["Cache-Control"] = "no-store"
    try:
        captured = datetime.fromisoformat(checked_at.replace("Z", "+00:00"))
    except ValueError as exc:
        raise HTTPException(status_code=422, detail="invalid_audit_cutoff") from exc
    if captured.tzinfo is None:
        raise HTTPException(status_code=422, detail="invalid_audit_cutoff_timezone")
    cutoff = captured.astimezone(timezone.utc)
    now = datetime.now(timezone.utc)
    if cutoff.date() > now.date() or (now.date() - cutoff.date()).days > 2:
        raise HTTPException(status_code=422, detail="audit_cutoff_out_of_range")
    if provider == "b3":
        current = monotonic()
        with _PROBE_LOCK:
            if _last_b3_probe_started is not None and current - _last_b3_probe_started < 30:
                raise HTTPException(status_code=429, detail="b3_probe_cooldown", headers={"Retry-After": "30"})
            _last_b3_probe_started = current
    return collect_provider_probe(provider, checked_at, environment="application")


def _source_object(value: object) -> dict[str, object] | None:
    if not isinstance(value, dict):
        return None
    result: dict[str, object] = {}
    for key, item in cast(dict[object, object], value).items():
        if not isinstance(key, str):
            return None
        result[key] = item
    return result
