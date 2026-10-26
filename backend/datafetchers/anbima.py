from datetime import datetime, timezone
from io import BytesIO

import httpx
from python_calamine import CalamineError, CalamineWorkbook

from config import ANBIMA_XLS_URL
from db.cached_reads import query_anbima_all
from db.store import get_meta, query_anbima, set_meta, upsert_anbima
from ingestion_policy import ErrorCategory, classify_exception, current_retry_budget
from provider_hosts import ProviderHostRejectedError
from provider_http import (
    BINARY_MAX_BYTES,
    ProviderPayloadTooLargeError,
    get_provider_response,
)
from source_ingestion import acquisition_unit, audited


class _AnbimaErrorPayload(dict[str, object]):
    """Legacy JSON shape with private classification metadata for the adapter."""

    def __init__(self, message: str, category: ErrorCategory, stage: str) -> None:
        super().__init__(error=message)
        self.error_category = category
        self.stage = stage


def _needs_refresh() -> bool:
    last = get_meta("anbima_last_refresh")
    if not last:
        return True
    try:
        last_dt = datetime.fromisoformat(last)
        if last_dt.tzinfo is None:
            last_dt = last_dt.replace(tzinfo=timezone.utc)
        return (datetime.now(timezone.utc) - last_dt.astimezone(timezone.utc)).total_seconds() > 23 * 3600
    except (TypeError, ValueError, OverflowError):
        return True


def _fetch_anbima_ima(use_cache: bool = True) -> dict[str, object]:
    if use_cache and not _needs_refresh():
        cached = query_anbima()
        if cached:
            return cached

    try:
        budget = current_retry_budget()
        request_options = {
            "request": httpx.get,
            "timeout": 120,
            "max_bytes": BINARY_MAX_BYTES,
            "headers": {
                "User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36",
                "Accept": "application/vnd.ms-excel,*/*",
            },
        }
        if budget is None:
            resp = get_provider_response(ANBIMA_XLS_URL, **request_options)
        else:
            with acquisition_unit("anbima:ima_workbook", 3, 120.0):
                resp = get_provider_response(ANBIMA_XLS_URL, **request_options)
        xls_bytes = BytesIO(resp.content)
        wb = CalamineWorkbook.from_filelike(xls_bytes)

        for sheet_name in wb.sheet_names:
            clean_name = sheet_name.strip().lower().replace(" ", "_").replace("-", "_")
            sheet = wb.get_sheet_by_name(sheet_name)
            raw = sheet.to_python()
            if not raw:
                continue
            header = [str(h) if h else f"col_{i}" for i, h in enumerate(raw[0])]
            records = []
            for row in raw[1:]:
                rec = {}
                for i, h in enumerate(header):
                    val = row[i] if i < len(row) else None
                    if val == "" or val is None:
                        rec[h] = None
                    elif hasattr(val, "isoformat"):
                        rec[h] = val.isoformat()
                    else:
                        rec[h] = val
                records.append(rec)
            upsert_anbima(clean_name, records)

        set_meta("anbima_last_refresh", datetime.now(timezone.utc).isoformat())
        return query_anbima()

    except (
        httpx.HTTPError, ProviderHostRejectedError, ProviderPayloadTooLargeError,
        CalamineError, ValueError, TypeError, OSError, RuntimeError,
    ) as e:
        category = ErrorCategory.INVALID_SCHEMA if isinstance(e, CalamineError) else classify_exception(e)
        stage = "validation" if category is ErrorCategory.INVALID_SCHEMA else "network"
        return _AnbimaErrorPayload(str(e), category, stage)


fetch_anbima_ima = audited("anbima", "ima_history", qualifiers=lambda p: {
    "dataset": "IMA historical workbook",
}, network_expected=lambda p: _network_expected(p))(_fetch_anbima_ima)


def _network_expected(params: dict[str, object]) -> bool:
    return not bool(params.get("use_cache", True)) or _needs_refresh() or not bool(query_anbima())


def read_anbima_ima() -> dict[str, list[dict]]:
    return query_anbima_all()


def force_refresh_anbima() -> dict:
    set_meta("anbima_last_refresh", "")
    return fetch_anbima_ima(use_cache=False)
