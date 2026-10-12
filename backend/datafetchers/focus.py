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


def _needs_refresh() -> bool:
    last = get_meta("focus_last_refresh")
    if not last:
        return True
    try:
        last_dt = datetime.fromisoformat(last)
        return (datetime.now() - last_dt).total_seconds() > 23 * 3600
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


def fetch_focus(
    indicator: str,
    top_n: int = 200,
    use_cache: bool = True,
) -> list[dict]:
    if use_cache and not _needs_refresh():
        cached = query_focus(indicator)
        if cached and indicator in cached and cached[indicator]:
            return cached[indicator]

    url = f"{BCB_ODATA_BASE}/ExpectativasMercadoAnuais"
    provider_indicator = PROVIDER_INDICATORS.get(indicator, indicator)
    params = {
        "$filter": f"Indicador eq '{provider_indicator}'",
        "$orderby": "Data%20desc",
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
        data = resp.json().get("value", [])
        if data:
            upsert_focus(indicator, data)
            set_meta("focus_last_refresh", datetime.now().isoformat())
    except Exception as e:
        print(f"[FOCUS] Erro indicator {indicator}: {e}")
        data = []

    cached = query_focus(indicator)
    if cached and indicator in cached:
        return cached[indicator]
    return data


def read_focus(indicator: str) -> list[dict]:
    return query_focus_batch([indicator])[indicator]


def read_all_focus() -> dict[str, list[dict]]:
    return query_focus_batch(INDICATORS)


def read_focus_12m() -> dict[str, list[FocusRecord]]:
    return cast(dict[str, list[FocusRecord]], query_focus_batch([FOCUS_12M_KEY]))


def fetch_focus_12m(
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


def fetch_all_focus(use_cache: bool = True) -> dict[str, object]:
    result = {}
    for ind in INDICATORS:
        try:
            result[ind] = fetch_focus(ind, use_cache=use_cache)
        except Exception as e:
            result[ind] = {"error": str(e)}
    return result


def force_refresh_focus() -> dict:
    set_meta("focus_last_refresh", "")
    return fetch_all_focus(use_cache=False)
