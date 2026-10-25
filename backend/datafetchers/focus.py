import json
from dataclasses import dataclass
from datetime import date, datetime, timezone
from math import isfinite
from typing import TypeAlias, cast

import httpx

from config import BCB_ODATA_BASE
from db.cached_reads import query_focus_batch
from db.store import get_meta, query_focus, set_meta, upsert_focus
from provider_hosts import ProviderHostRejectedError
from provider_http import (
    JSON_MAX_BYTES,
    ProviderPayloadTooLargeError,
    get_provider_response,
)
from source_ingestion import (
    FetchResult,
    SourceOperationError,
    as_error_payload,
    audited,
)

INDICATORS = ["IPCA", "Selic", "PIB", "Câmbio", "IGP-M"]
PROVIDER_INDICATORS = {"PIB": "PIB Total"}
FOCUS_12M_KEY = "IPCA_12M_S_BASE0"
FOCUS_12M_FILTER = "Indicador eq 'IPCA' and Suavizada eq 'S' and baseCalculo eq 0"
FOCUS_12M_META_KEY = "focus_12m_last_refresh"
FocusRecord: TypeAlias = dict[str, object]


@dataclass(frozen=True, slots=True)
class Focus12MRefreshError(RuntimeError):
    reason: str

    def __str__(self) -> str:
        return f"Focus 12-month refresh failed: {self.reason}"


class FocusAnnualRefreshError(SourceOperationError):
    """A Focus annual-family attempt failed and must not advance freshness."""

    def __init__(
        self,
        reason: str,
        *,
        stage: str = "network",
        received_count: int | None = None,
        valid_count: int | None = None,
        persisted_count: int | None = None,
        committed_count: int | None = None,
        source_data_date: str | None = None,
    ) -> None:
        self.reason = reason
        super().__init__(
            reason,
            stage=stage,
            received_count=received_count,
            valid_count=valid_count,
            persisted_count=persisted_count,
            committed_count=committed_count,
            source_data_date=source_data_date,
        )


def _annual_meta_key(indicator: str) -> str:
    return f"focus_annual_last_refresh_{indicator}"


def _needs_refresh(indicator: str = "IPCA") -> bool:
    last = get_meta(_annual_meta_key(indicator))
    if not last:
        return True
    try:
        last_dt = datetime.fromisoformat(last)
        if last_dt.tzinfo is None:
            last_dt = last_dt.replace(tzinfo=timezone.utc)
        return (datetime.now(timezone.utc) - last_dt.astimezone(timezone.utc)).total_seconds() > 23 * 3600
    except (TypeError, ValueError):
        return True


def _focus_12m_needs_refresh() -> bool:
    last = get_meta(FOCUS_12M_META_KEY)
    if not last:
        return True
    try:
        last_dt = datetime.fromisoformat(last)
        return (datetime.now(timezone.utc) - last_dt).total_seconds() > 23 * 3600
    except (TypeError, ValueError):
        return True


def _validated_focus_12m_records(value: object) -> list[FocusRecord] | None:
    if not isinstance(value, list) or not value:
        return None
    records: list[dict[str, object]] = []
    for item in value:
        if not isinstance(item, dict):
            return None
        record = cast(FocusRecord, item)
        try:
            json.dumps(record, allow_nan=False)
        except (TypeError, ValueError):
            return None
        raw_date = record.get("Data")
        median = record.get("Mediana")
        base_calculation = record.get("baseCalculo")
        if (
            record.get("Indicador") != "IPCA"
            or record.get("Suavizada") != "S"
            or isinstance(base_calculation, bool)
            or base_calculation != 0
            or not isinstance(raw_date, str)
            or isinstance(median, bool)
            or not isinstance(median, (int, float))
            or not isfinite(float(median))
        ):
            return None
        try:
            date.fromisoformat(raw_date[:10])
        except ValueError:
            return None
        records.append(record)
    return records


def _fetch_focus(
    indicator: str,
    top_n: int = 200,
    use_cache: bool = True,
) -> list[dict]:
    if use_cache and not _needs_refresh(indicator):
        cached = query_focus(indicator)
        if cached and indicator in cached and cached[indicator]:
            return cached[indicator]

    url = f"{BCB_ODATA_BASE}/ExpectativasMercadoAnuais"
    provider_indicator = PROVIDER_INDICATORS.get(indicator, indicator)
    params = {
        "$filter": f"Indicador eq '{provider_indicator}'",
        "$orderby": "Data desc",
        "$top": str(top_n),
        "$format": "json",
    }

    try:
        resp = get_provider_response(
            url,
            request=httpx.get,
            params=params,
            timeout=30,
            max_bytes=JSON_MAX_BYTES,
        )
    except Exception as error:
        raise FocusAnnualRefreshError("provider_failure", stage="network") from error
    try:
        payload = resp.json()
    except (TypeError, ValueError) as error:
        raise FocusAnnualRefreshError("invalid_payload", stage="validation") from error
    data = payload.get("value") if isinstance(payload, dict) else None
    if not isinstance(data, list) or not _valid_annual_records(data, indicator):
        raise FocusAnnualRefreshError("invalid_payload", stage="validation")
    source_data_date = max(record["Data"][:10] for record in data if isinstance(record.get("Data"), str))
    try:
        upsert_focus(indicator, data)
    except Exception as error:
        raise FocusAnnualRefreshError(
            "persistence_failure", stage="persistence", received_count=len(data),
            valid_count=len(data), source_data_date=source_data_date,
        ) from error
    timestamp = datetime.now(timezone.utc).isoformat()
    try:
        set_meta(_annual_meta_key(indicator), timestamp)
        set_meta("focus_last_refresh", timestamp)  # legacy diagnostic compatibility only.
    except Exception as error:
        raise FocusAnnualRefreshError(
            "freshness_write_failure", stage="freshness", received_count=len(data),
            valid_count=len(data), persisted_count=len(data), committed_count=len(data),
            source_data_date=source_data_date,
        ) from error
    return FetchResult(
        cast(list[dict], data),
        network_called=True,
        received_count=len(data),
        valid_count=len(data),
        persisted_count=len(data),
        request_count=1,
    )


def _valid_annual_records(data: list[object], indicator: str) -> bool:
    if not data:
        return False
    today = datetime.now(timezone.utc).date()
    for item in data:
        if not isinstance(item, dict):
            return False
        record = cast(dict[str, object], item)
        raw_date = record.get("Data")
        target_year = record.get("DataReferencia")
        base = record.get("baseCalculo")
        median = record.get("Mediana")
        try:
            valid_date = isinstance(raw_date, str) and date.fromisoformat(raw_date[:10]) <= today
            valid_year = int(str(target_year)) > 2000
            json.dumps(record, allow_nan=False)
        except (TypeError, ValueError):
            return False
        if (not valid_date or not valid_year or record.get("Indicador") != PROVIDER_INDICATORS.get(indicator, indicator)
                or isinstance(base, bool) or base not in (0, 1)
                or isinstance(median, bool) or not isinstance(median, (int, float))
                or not isfinite(float(median))):
            return False
    return True


fetch_focus = audited("focus", "indicator", qualifiers=lambda p: {
    "kind": "annual", "indicator": p.get("indicator"), "top_n": p.get("top_n", 200),
}, network_expected=lambda p: _annual_network_expected(p))(_fetch_focus)


def _annual_network_expected(params: dict[str, object]) -> bool:
    indicator = str(params.get("indicator", ""))
    if not bool(params.get("use_cache", True)) or _needs_refresh(indicator):
        return True
    cached = query_focus(indicator)
    return not bool(cached and cached.get(indicator))


def read_focus(indicator: str) -> list[dict]:
    return query_focus_batch([indicator])[indicator]


def read_all_focus() -> dict[str, list[dict]]:
    return query_focus_batch(INDICATORS)


def read_focus_12m() -> dict[str, list[FocusRecord]]:
    return cast(dict[str, list[FocusRecord]], query_focus_batch([FOCUS_12M_KEY]))


def _fetch_focus_12m(
    top_n: int = 200,
    use_cache: bool = True,
) -> dict[str, list[FocusRecord]]:
    if use_cache and not _focus_12m_needs_refresh():
        return read_focus_12m()

    url = f"{BCB_ODATA_BASE}/ExpectativasMercadoInflacao12Meses"
    params = {
        "$filter": FOCUS_12M_FILTER,
        "$orderby": "Data desc",
        "$top": str(top_n),
        "$format": "json",
    }
    try:
        response = get_provider_response(
            url,
            request=httpx.get,
            params=params,
            timeout=30,
            max_bytes=JSON_MAX_BYTES,
        )
    except (httpx.HTTPError, ProviderHostRejectedError, ProviderPayloadTooLargeError) as error:
        raise Focus12MRefreshError(reason="provider_failure") from error
    try:
        payload = response.json()
    except (TypeError, ValueError) as error:
        raise Focus12MRefreshError(reason="invalid_payload") from error
    value = payload.get("value") if isinstance(payload, dict) else None
    records = _validated_focus_12m_records(value)
    if records is None:
        raise Focus12MRefreshError(reason="invalid_payload")
    upsert_focus(FOCUS_12M_KEY, records)
    set_meta(FOCUS_12M_META_KEY, datetime.now(timezone.utc).isoformat())
    return read_focus_12m()


fetch_focus_12m = audited("focus", "rolling_12m", qualifiers=lambda p: {
    "kind": "rolling_12m", "filter": FOCUS_12M_FILTER, "top_n": p.get("top_n", 200),
}, network_expected=lambda p: _rolling_network_expected(p))(_fetch_focus_12m)


def _rolling_network_expected(params: dict[str, object]) -> bool:
    return not bool(params.get("use_cache", True)) or _focus_12m_needs_refresh()


def fetch_all_focus(use_cache: bool = True) -> dict[str, object]:
    result = {}
    for ind in INDICATORS:
        try:
            result[ind] = fetch_focus(ind, use_cache=use_cache)
        except FocusAnnualRefreshError as error:
            result[ind] = as_error_payload(error)
    return result


def force_refresh_focus() -> dict:
    set_meta("focus_last_refresh", "")
    return fetch_all_focus(use_cache=False)
