import json
from dataclasses import dataclass
from datetime import date, datetime
from math import isfinite
from time import monotonic, sleep

import httpx

from config import BCB_SGS_BASE
from db.cached_reads import query_sgs_batch
from db.store import (
    get_meta,
    get_sgs_range,
    query_sgs,
    query_sgs_latest,
    set_meta,
    upsert_sgs,
)
from domain_contracts import Record, SeriesMap
from provider_http import (
    JSON_MAX_BYTES,
    ProviderInvalidJSONResponseError,
    get_provider_response,
    get_provider_response_bounded,
)

MAX_RANGE_YEARS = 10
REQUEST_TIMEOUT = 30
# A ten-year IPCA-group window has about 120 monthly observations; 256 KiB
# leaves a wide margin while bounding buffered JSON decoding per work item.
SGS_WINDOW_MAX_BYTES = 256 * 1024
SGS_JSON_RETRY_DELAYS = (0.25, 0.75)
KNOWN_UNAVAILABLE = set()

# Series codes that return "dataFim" in addition to "data"
# These represent monthly rates with start/end date pairs
MONTHLY_RATE_SERIES = {226}  # TR


@dataclass(slots=True)
class InvalidSGSPayloadError(ValueError):
    series_code: int
    start_date: str | None
    end_date: str | None
    reason: str
    record_index: int | None = None

    @property
    def error_code(self) -> str:
        window = f"{self.start_date or 'start'}-{self.end_date or 'end'}"
        row = f":row{self.record_index}" if self.record_index is not None else ""
        return f"InvalidSGSPayload:{self.series_code}:{window}:{self.reason}{row}"[:80]

    def __str__(self) -> str:
        window = f"{self.start_date or 'start'}-{self.end_date or 'end'}"
        row = f" row {self.record_index}" if self.record_index is not None else ""
        return f"invalid SGS payload for series {self.series_code} window {window}{row}: {self.reason}"


def _parse_date(d: str) -> date:
    parts = d.split("/")
    return date(int(parts[2]), int(parts[1]), int(parts[0]))


def _format_date(d: date) -> str:
    return d.strftime("%d/%m/%Y")


def _validate_sgs_payload(
    payload: object,
    series_code: int,
    start_date: str | None,
    end_date: str | None,
) -> list[Record]:
    if not isinstance(payload, list):
        raise InvalidSGSPayloadError(series_code, start_date, end_date, "expected-list")
    records: list[Record] = []
    for index, item in enumerate(payload):
        if not isinstance(item, dict):
            raise InvalidSGSPayloadError(series_code, start_date, end_date, "expected-record", index)
        observation_date = item.get("data")
        if not isinstance(observation_date, str):
            raise InvalidSGSPayloadError(series_code, start_date, end_date, "invalid-date", index)
        try:
            parsed_date = datetime.strptime(observation_date, "%d/%m/%Y")
        except ValueError as exc:
            raise InvalidSGSPayloadError(
                series_code, start_date, end_date, "invalid-date", index
            ) from exc
        if parsed_date.strftime("%d/%m/%Y") != observation_date:
            raise InvalidSGSPayloadError(series_code, start_date, end_date, "invalid-date", index)
        value = item.get("valor")
        if not isinstance(value, str) or not value.strip():
            raise InvalidSGSPayloadError(series_code, start_date, end_date, "invalid-value", index)
        try:
            numeric_value = float(value.replace(",", "."))
        except ValueError as exc:
            raise InvalidSGSPayloadError(
                series_code, start_date, end_date, "invalid-value", index
            ) from exc
        if not isfinite(numeric_value):
            raise InvalidSGSPayloadError(series_code, start_date, end_date, "invalid-value", index)
        records.append(item)
    return records


def _fetch_bounded_sgs_json(
    url: str,
    params: dict[str, str],
    deadline_seconds: float,
) -> object:
    deadline = monotonic() + deadline_seconds
    for attempt in range(len(SGS_JSON_RETRY_DELAYS) + 1):
        remaining = deadline - monotonic()
        if remaining <= 0:
            raise TimeoutError("SGS request deadline elapsed before retry")
        response = get_provider_response_bounded(
            url,
            params=params,
            deadline_seconds=remaining,
            max_bytes=SGS_WINDOW_MAX_BYTES,
        )
        try:
            return response.json()
        except json.JSONDecodeError as cause:
            error = ProviderInvalidJSONResponseError(
                status_code=response.status_code,
                content_type=response.headers.get("content-type"),
                body_bytes=len(response.content),
            )
            if attempt >= len(SGS_JSON_RETRY_DELAYS):
                raise error from cause
            delay = SGS_JSON_RETRY_DELAYS[attempt]
            remaining = deadline - monotonic()
            if remaining <= delay:
                raise error from cause
            sleep(delay)
    raise AssertionError("unreachable SGS JSON retry state")


def _make_windows(start: str | None, end: str | None) -> list[tuple[str | None, str | None]]:
    if not start:
        return [(None, end)]
    s = _parse_date(start)
    e = _parse_date(end) if end else date.today()
    windows = []
    cursor = s
    while cursor < e:
        window_end = min(cursor.replace(year=cursor.year + MAX_RANGE_YEARS), e)
        windows.append((_format_date(cursor), _format_date(window_end)))
        cursor = window_end
    return windows if windows else [(start, end)]


def sgs_refresh_windows(
    start_date: str | None = None,
    end_date: str | None = None,
) -> tuple[tuple[str | None, str | None], ...]:
    """Return the bounded date windows used for a full SGS refresh."""
    return tuple(_make_windows(start_date or "01/01/1990", end_date))


def _needs_refresh(series_code: int) -> bool:
    key = f"sgs_last_refresh_{series_code}"
    last = get_meta(key)
    if not last:
        return True
    try:
        last_dt = datetime.fromisoformat(last)
        return (datetime.now() - last_dt).total_seconds() > 23 * 3600
    except Exception:
        return True


def fetch_sgs_series(
    code: int,
    start_date: str | None = None,
    end_date: str | None = None,
    use_cache: bool = True,
    *,
    mark_refreshed: bool = True,
    fail_fast: bool = False,
    request_timeout: float = REQUEST_TIMEOUT,
) -> list[Record]:
    if code in KNOWN_UNAVAILABLE:
        return query_sgs(code, start_date, end_date)

    db_min, db_max = get_sgs_range(code)

    if use_cache and not _needs_refresh(code) and db_min:
        return query_sgs(code, start_date, end_date)

    effective_start = start_date or "01/01/1990"

    windows = _make_windows(effective_start, end_date)
    all_data: list[Record] = []

    for w_start, w_end in windows:
        url = f"{BCB_SGS_BASE.format(code=code)}?formato=json"
        params = {}
        if w_start:
            params["dataInicial"] = w_start
        if w_end:
            params["dataFinal"] = w_end
        try:
            if request_timeout == REQUEST_TIMEOUT:
                response = get_provider_response(
                    url,
                    request=httpx.get,
                    params=params,
                    timeout=request_timeout,
                    max_bytes=JSON_MAX_BYTES,
                )
                chunk = response.json()
            else:
                chunk = _fetch_bounded_sgs_json(url, params, request_timeout)
            chunk = _validate_sgs_payload(chunk, code, w_start, w_end)
            if code in MONTHLY_RATE_SERIES:
                chunk = [{"data": r.get("data"), "valor": r.get("valor")} for r in chunk if r.get("data") and r.get("valor")]
            all_data.extend(chunk)
        except InvalidSGSPayloadError:
            raise
        except Exception as e:
            if fail_fast:
                raise
            print(f"[SGS] Erro serie {code} janela {w_start}-{w_end}: {e}")

    if all_data:
        upsert_sgs(code, all_data)
        if mark_refreshed:
            now_iso = datetime.now().isoformat()
            set_meta(f"sgs_last_refresh_{code}", now_iso)
            set_meta("last_successful_update", now_iso)

    if fail_fast and not all_data:
        raise RuntimeError(f"SGS series {code} window returned no usable observations")

    cached = query_sgs(code, start_date, end_date)
    if cached:
        return cached

    return all_data


def read_sgs_series(
    code: int,
    start_date: str | None = None,
    end_date: str | None = None,
) -> list[Record]:
    return query_sgs(code, start_date, end_date)


def read_sgs_batch(
    codes: dict[str, int],
    start_date: str | None = None,
    end_date: str | None = None,
) -> SeriesMap:
    return query_sgs_batch(codes, start_date, end_date)


def fetch_sgs_last_n(code: int, n: int = 10) -> list[Record]:
    if code in KNOWN_UNAVAILABLE:
        return query_sgs_latest(code, n)

    if not _needs_refresh(code):
        cached = query_sgs_latest(code, n)
        if cached:
            return cached

    url = f"{BCB_SGS_BASE.format(code=code)}/ultimos/{n}?formato=json"
    try:
        resp = get_provider_response(
            url,
            request=httpx.get,
            timeout=REQUEST_TIMEOUT,
            max_bytes=JSON_MAX_BYTES,
        )
        data = resp.json()
        upsert_sgs(code, data)
        set_meta(f"sgs_last_refresh_{code}", datetime.now().isoformat())
    except Exception as e:
        print(f"[SGS] Erro last_n serie {code}: {e}")

    return query_sgs_latest(code, n)


def fetch_sgs_batch(
    codes: dict[str, int],
    start_date: str | None = None,
    use_cache: bool = True,
    *,
    end_date: str | None = None,
    mark_refreshed: bool = True,
    fail_fast: bool = False,
    request_timeout: float = REQUEST_TIMEOUT,
) -> dict[str, object]:
    result = {}
    for name, code in codes.items():
        try:
            options: dict[str, object] = {}
            if end_date is not None:
                options["end_date"] = end_date
            if not mark_refreshed:
                options["mark_refreshed"] = False
            if fail_fast:
                options["fail_fast"] = True
            if request_timeout != REQUEST_TIMEOUT:
                options["request_timeout"] = request_timeout
            result[name] = fetch_sgs_series(
                code,
                start_date=start_date,
                use_cache=use_cache,
                **options,
            )
        except Exception as e:
            if fail_fast:
                raise
            result[name] = {"error": str(e)}
    return result


def force_refresh_sgs(code: int) -> list[Record]:
    key = f"sgs_last_refresh_{code}"
    set_meta(key, "")
    return fetch_sgs_series(code, use_cache=False)
