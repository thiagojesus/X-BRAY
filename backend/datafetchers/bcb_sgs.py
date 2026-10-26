import json
from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone
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
    query_tr_periods,
    set_meta,
    upsert_sgs,
    upsert_tr_periods,
)
from domain_contracts import Record, SeriesMap
from ingestion_policy import ErrorCategory, classify_exception, current_retry_budget
from provider_http import (
    JSON_MAX_BYTES,
    ProviderInvalidJSONResponseError,
    get_provider_response,
    get_provider_response_bounded,
)
from source_ingestion import FetchResult, acquisition_unit, audited

MAX_RANGE_YEARS = 10
REQUEST_TIMEOUT = 30
# A ten-year IPCA-group window has about 120 monthly observations; 256 KiB
# leaves a wide margin while bounding buffered JSON decoding per work item.
SGS_WINDOW_MAX_BYTES = 256 * 1024
SGS_JSON_RETRY_DELAYS = (0.25, 0.75)
KNOWN_UNAVAILABLE = set()


class SGSWindowFetchError(RuntimeError):
    """A source window failed; retain the underlying provider exception class."""

    stage = "network"

    def __init__(self, code: int, failures: list[tuple[str | None, str | None, Exception]]) -> None:
        self.provider_error_code = type(failures[0][2]).__name__
        self.reason = self.provider_error_code
        self.error_category = classify_exception(failures[0][2])
        windows = ", ".join(f"{start}-{end}" for start, end, _ in failures)
        super().__init__(
            f"SGS series {code} had {len(failures)} unsuccessful window(s): {windows}"
        )

# Series codes that return "dataFim" in addition to "data"
# These represent monthly rates with start/end date pairs
MONTHLY_RATE_SERIES = {226}  # TR


@dataclass(slots=True)
class InvalidSGSPayloadError(ValueError):
    error_category = ErrorCategory.INVALID_SCHEMA
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
            parsed_date = datetime.strptime(observation_date, "%d/%m/%Y").replace(tzinfo=timezone.utc)
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


def _normalize_window_records(
    records: list[Record],
    series_code: int,
    start_date: str | None,
    end_date: str | None,
) -> list[Record]:
    """Reject out-of-window rows and collapse only identical duplicate dates."""
    start = _parse_window_date(start_date) if start_date else None
    end = _parse_window_date(end_date) if end_date else None
    by_date: dict[date, Record] = {}
    by_period: dict[tuple[date, date], Record] = {}
    for index, record in enumerate(records):
        raw_date = record.get("data")
        if not isinstance(raw_date, str):
            raise InvalidSGSPayloadError(series_code, start_date, end_date, "invalid-date", index)
        observed = _parse_date(raw_date)
        if (start is not None and observed < start) or (end is not None and observed > end):
            raise InvalidSGSPayloadError(series_code, start_date, end_date, "outside-requested-window", index)
        if series_code in MONTHLY_RATE_SERIES:
            raw_end = record.get("dataFim")
            if not isinstance(raw_end, str):
                raise InvalidSGSPayloadError(series_code, start_date, end_date, "invalid-period-end", index)
            try:
                period_end = _parse_date(raw_end)
            except (ValueError, IndexError) as exc:
                raise InvalidSGSPayloadError(series_code, start_date, end_date, "invalid-period-end", index) from exc
            if period_end <= observed:
                raise InvalidSGSPayloadError(series_code, start_date, end_date, "invalid-period", index)
            period_key = (observed, period_end)
            prior_period = by_period.get(period_key)
            if prior_period is not None:
                if float(str(prior_period.get("valor")).replace(",", ".")) != float(str(record.get("valor")).replace(",", ".")):
                    raise InvalidSGSPayloadError(series_code, start_date, end_date, "conflicting-duplicate-period", index)
                continue
            by_period[period_key] = record
            continue
        prior = by_date.get(observed)
        if prior is not None:
            if float(str(prior.get("valor")).replace(",", ".")) != float(
                str(record.get("valor")).replace(",", ".")
            ):
                raise InvalidSGSPayloadError(series_code, start_date, end_date, "conflicting-duplicate-date", index)
            continue
        by_date[observed] = record
    if series_code in MONTHLY_RATE_SERIES:
        return [by_period[key] for key in sorted(by_period)]
    return [by_date[observed] for observed in sorted(by_date)]


def parse_sgs_shadow_primary_payload(payload: object, series_code: int) -> list[Record]:
    """Pure validation and normalization for a read-only SGS shadow probe."""
    records = _validate_sgs_payload(payload, series_code, None, None)
    return _normalize_window_records(records, series_code, None, None)


def sgs_annual_windows(start_date: str, end_date: str) -> tuple[tuple[str, str], ...]:
    """Freeze inclusive annual source windows between validated ISO or legacy dates."""
    start = _parse_window_date(start_date)
    end = _parse_window_date(end_date)
    if end < start:
        raise ValueError("SGS window end precedes its start")
    windows: list[tuple[str, str]] = []
    cursor = start
    while cursor <= end:
        year_end = date(cursor.year, 12, 31)
        window_end = min(year_end, end)
        windows.append((_format_date(cursor), _format_date(window_end)))
        cursor = window_end + timedelta(days=1)
    return tuple(windows)


def _parse_window_date(value: str) -> date:
    try:
        return date.fromisoformat(value)
    except ValueError:
        day, month, year = (int(part) for part in value.split("/"))
        return date(year, month, day)


def _fetch_bounded_sgs_json(
    url: str,
    params: dict[str, str],
    deadline_seconds: float,
) -> object:
    deadline = monotonic() + deadline_seconds
    last_error: Exception | None = None
    for attempt in range(3):
        remaining = deadline - monotonic()
        if remaining <= 0:
            raise TimeoutError("SGS request deadline elapsed before retry") from last_error
        try:
            response = get_provider_response_bounded(
                url, params=params, deadline_seconds=remaining,
                max_bytes=SGS_WINDOW_MAX_BYTES, max_attempts=1,
                max_retry_wait_seconds=1.0,
            )
        except json.JSONDecodeError as cause:
            last_error = cause
            response = None
        except (httpx.TransportError, httpx.HTTPStatusError) as error:
            last_error = error
            response = None
        if response is not None:
            try:
                return response.json()
            except json.JSONDecodeError as cause:
                last_error = ProviderInvalidJSONResponseError(
                    status_code=response.status_code,
                    content_type=response.headers.get("content-type"),
                    body_bytes=len(response.content),
                )
                last_error.__cause__ = cause
        if attempt < 2:
            delay = SGS_JSON_RETRY_DELAYS[min(attempt, len(SGS_JSON_RETRY_DELAYS) - 1)]
            if deadline - monotonic() <= delay:
                break
            sleep(delay)
    if isinstance(last_error, ProviderInvalidJSONResponseError):
        raise last_error
    if last_error is not None:
        raise last_error
    raise TimeoutError("SGS request deadline elapsed before response")


def _fetch_sgs_json(url: str, params: dict[str, str], timeout: float) -> object:
    """Retry legacy SGS calls at the same instrumentable HTTP boundary."""
    deadline = monotonic() + timeout
    last_error: Exception | None = None
    for attempt in range(3):
        remaining = deadline - monotonic()
        if remaining <= 0:
            raise TimeoutError("SGS request deadline elapsed before retry") from last_error
        try:
            response = get_provider_response(
                url, request=httpx.get, params=params, timeout=remaining,
                max_bytes=JSON_MAX_BYTES,
            )
            try:
                return response.json()
            except json.JSONDecodeError as cause:
                last_error = ProviderInvalidJSONResponseError(
                    status_code=response.status_code,
                    content_type=response.headers.get("content-type"),
                    body_bytes=len(response.content),
                )
                last_error.__cause__ = cause
        except (httpx.TransportError, httpx.HTTPStatusError) as error:
            if isinstance(error, httpx.HTTPStatusError) and error.response.status_code < 500:
                raise
            last_error = error
        if attempt < 2:
            delay = SGS_JSON_RETRY_DELAYS[min(attempt, len(SGS_JSON_RETRY_DELAYS) - 1)]
            if deadline - monotonic() <= delay:
                break
            sleep(delay)
    if last_error is not None:
        raise last_error
    raise TimeoutError("SGS request deadline elapsed before response")


def _make_windows(start: str | None, end: str | None) -> list[tuple[str | None, str | None]]:
    if not start:
        return [(None, end)]
    s = _parse_date(start)
    e = _parse_date(end) if end else datetime.now(timezone.utc).date()
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
        if last_dt.tzinfo is None:
            last_dt = last_dt.replace(tzinfo=timezone.utc)
        return (datetime.now(timezone.utc) - last_dt.astimezone(timezone.utc)).total_seconds() > 23 * 3600
    except (TypeError, ValueError, OverflowError):
        return True


def _fetch_sgs_series(
    code: int,
    start_date: str | None = None,
    end_date: str | None = None,
    use_cache: bool = True,
    *,
    mark_refreshed: bool = True,
    fail_fast: bool = False,
    request_timeout: float = REQUEST_TIMEOUT,
    count_sink: list[int] | None = None,
    valid_count_sink: list[int] | None = None,
    persisted_count_sink: list[int | None] | None = None,
    reference_sink: list[str] | None = None,
) -> list[Record]:
    if code in KNOWN_UNAVAILABLE:
        return query_tr_periods(start_date, end_date) if code in MONTHLY_RATE_SERIES else query_sgs(code, start_date, end_date)

    db_min, _db_max = get_sgs_range(code)

    if code not in MONTHLY_RATE_SERIES and use_cache and not _needs_refresh(code) and db_min:
        return query_sgs(code, start_date, end_date)

    effective_start = start_date or "01/01/1990"

    windows = _make_windows(effective_start, end_date)
    budget = current_retry_budget()
    if budget is not None:
        budget.set_request_limit(len(windows) * 3)
        unit_deadline = 20.0 if budget.profile.deadline_seconds == 20.0 else 220.0
    all_data: list[Record] = []
    failed_windows: list[tuple[str | None, str | None, Exception]] = []

    for w_start, w_end in windows:
        url = f"{BCB_SGS_BASE.format(code=code)}?formato=json"
        params = {}
        if w_start:
            params["dataInicial"] = w_start
        if w_end:
            params["dataFinal"] = w_end
        try:
            unit = f"bcb_sgs:{code}:{w_start or 'start'}:{w_end or 'end'}"
            if request_timeout == REQUEST_TIMEOUT:
                fetch_json = lambda url=url, params=params: _fetch_sgs_json(
                    url, params, request_timeout
                )
            else:
                fetch_json = lambda url=url, params=params: _fetch_bounded_sgs_json(
                    url, params, request_timeout
                )
            if budget is None:
                chunk = fetch_json()
            else:
                with acquisition_unit(unit, 3, unit_deadline):
                    chunk = fetch_json()
            chunk = _validate_sgs_payload(chunk, code, w_start, w_end)
            if count_sink is not None:
                count_sink.append(len(chunk))
            chunk = _normalize_window_records(chunk, code, w_start, w_end)
            if valid_count_sink is not None:
                valid_count_sink.append(len(chunk))
            if reference_sink is not None:
                reference_sink.extend(
                    _parse_window_date(record["data"]).isoformat()
                    for record in chunk
                    if isinstance(record.get("data"), str)
                )
            all_data.extend(chunk)
        except InvalidSGSPayloadError:
            raise
        except Exception as e:
            if fail_fast:
                raise
            failed_windows.append((w_start, w_end, e))
            print(f"[SGS] Erro serie {code} janela {w_start}-{w_end}: {e}")

    persisted_count: int | None = None
    if all_data:
        if code in MONTHLY_RATE_SERIES:
            persisted_count = upsert_tr_periods(all_data)
        else:
            upsert_sgs(code, all_data)
        if persisted_count_sink is not None:
            persisted_count_sink.append(persisted_count)
        if mark_refreshed and not failed_windows:
            now_iso = datetime.now(timezone.utc).isoformat()
            set_meta(f"sgs_last_refresh_{code}", now_iso)
            set_meta("last_successful_update", now_iso)

    if failed_windows:
        raise SGSWindowFetchError(code, failed_windows)

    if fail_fast and not all_data:
        raise RuntimeError(f"SGS series {code} window returned no usable observations")

    cached = query_tr_periods(start_date, end_date) if code in MONTHLY_RATE_SERIES else query_sgs(code, start_date, end_date)
    if cached:
        return cached

    return all_data


fetch_sgs_series = audited("bcb_sgs", "code", qualifiers=lambda p: {
    "series_code": p.get("code"), "start_date": p.get("start_date"),
    "end_date": p.get("end_date"), "window_count": len(sgs_refresh_windows(
        p.get("start_date"), p.get("end_date")
    )),
}, network_expected=lambda p: _series_network_expected(p),
   counts_known=False)(_fetch_sgs_series)


def _series_network_expected(params: dict[str, object]) -> bool:
    code = int(params.get("code", 0))
    if code in KNOWN_UNAVAILABLE:
        return False
    if code in MONTHLY_RATE_SERIES:
        return True
    if not bool(params.get("use_cache", True)) or _needs_refresh(code):
        return True
    cached_min, _ = get_sgs_range(code)
    return not bool(cached_min)


def _window_cache_key(code: int, start_date: str, end_date: str, *, period_identity: bool = False) -> str:
    start = _parse_window_date(start_date).isoformat()
    end = _parse_window_date(end_date).isoformat()
    version = "period_v1_" if period_identity else ""
    return f"sgs_window_success_{version}{code}_{start}_{end}"


def _window_cache_fresh(code: int, start_date: str, end_date: str, *, period_identity: bool = False) -> bool:
    value = get_meta(_window_cache_key(code, start_date, end_date, period_identity=period_identity))
    if not value:
        return False
    try:
        parsed = datetime.fromisoformat(value)
    except (TypeError, ValueError, OverflowError):
        return False
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    age = datetime.now(timezone.utc) - parsed.astimezone(timezone.utc)
    return timedelta(0) <= age <= timedelta(days=7)


def _fetch_sgs_window(
    code: int,
    start_date: str,
    end_date: str,
    use_cache: bool = True,
    *,
    fail_fast: bool = True,
    request_timeout: float = 20,
) -> FetchResult:
    period_identity = code in MONTHLY_RATE_SERIES
    cached = query_tr_periods(start_date, end_date) if period_identity else query_sgs(code, start_date, end_date)
    if use_cache and cached and _window_cache_fresh(code, start_date, end_date, period_identity=period_identity):
        return FetchResult(cached, network_called=False, received_count=0, valid_count=0, status="cache_hit")
    received_counts: list[int] = []
    valid_counts: list[int] = []
    persisted_counts: list[int | None] = []
    fetched_references: list[str] = []
    rows = _fetch_sgs_series(
        code,
        start_date=start_date,
        end_date=end_date,
        use_cache=False,
        mark_refreshed=False,
        fail_fast=fail_fast,
        request_timeout=request_timeout,
        count_sink=received_counts,
        valid_count_sink=valid_counts,
        persisted_count_sink=persisted_counts,
        reference_sink=fetched_references,
    )
    if not rows:
        raise RuntimeError(f"SGS series {code} window returned no observations")
    set_meta(_window_cache_key(code, start_date, end_date, period_identity=period_identity), datetime.now(timezone.utc).isoformat())
    return FetchResult(
        rows,
        network_called=True,
        received_count=sum(received_counts),
        valid_count=sum(valid_counts),
        persisted_count=sum(value for value in persisted_counts if value is not None) if persisted_counts else None,
        inserted_count=sum(value for value in persisted_counts if value is not None) if persisted_counts else None,
        source_data_date=max(fetched_references) if fetched_references else None,
        reference_min=min(fetched_references) if fetched_references else None,
        reference_max=max(fetched_references) if fetched_references else None,
    )


fetch_sgs_window = audited(
    "bcb_sgs",
    "code",
    qualifiers=lambda params: {
        "series_code": params.get("code"),
        "start_date": params.get("start_date"),
        "end_date": params.get("end_date"),
        "window_policy": "calendar-year-v1",
    },
    network_expected=lambda params: not (
        bool(params.get("use_cache", True))
        and _window_cache_fresh(
            int(params.get("code", 0)),
            str(params.get("start_date", "")),
            str(params.get("end_date", "")),
            period_identity=int(params.get("code", 0)) in MONTHLY_RATE_SERIES,
        )
        and bool(query_tr_periods(str(params.get("start_date", "")), str(params.get("end_date", "")))
                 if int(params.get("code", 0)) in MONTHLY_RATE_SERIES else query_sgs(
                     int(params.get("code", 0)), str(params.get("start_date", "")), str(params.get("end_date", ""))))
    ),
)(_fetch_sgs_window)


def read_sgs_series(
    code: int,
    start_date: str | None = None,
    end_date: str | None = None,
) -> list[Record]:
    if code in MONTHLY_RATE_SERIES:
        return query_tr_periods(start_date, end_date)
    return query_sgs(code, start_date, end_date)


def read_sgs_batch(
    codes: dict[str, int],
    start_date: str | None = None,
    end_date: str | None = None,
) -> SeriesMap:
    return query_sgs_batch(codes, start_date, end_date)


def _fetch_sgs_last_n(code: int, n: int = 10) -> list[Record]:
    if code in MONTHLY_RATE_SERIES:
        return query_tr_periods()
    if code in KNOWN_UNAVAILABLE:
        return query_sgs_latest(code, n)

    if not _needs_refresh(code):
        cached = query_sgs_latest(code, n)
        if cached:
            return cached

    url = f"{BCB_SGS_BASE.format(code=code)}/ultimos/{n}?formato=json"
    resp = get_provider_response(
        url,
        request=httpx.get,
        timeout=REQUEST_TIMEOUT,
        max_bytes=JSON_MAX_BYTES,
    )
    data = _validate_sgs_payload(resp.json(), code, None, None)
    upsert_sgs(code, data)
    set_meta(f"sgs_last_refresh_{code}", datetime.now(timezone.utc).isoformat())

    return query_sgs_latest(code, n)


fetch_sgs_last_n = audited(
    "bcb_sgs", "code", qualifiers=lambda params: {"series_code": params.get("code"), "last_n": params.get("n", 10)},
    network_expected=lambda params: _needs_refresh(int(params.get("code", 0)))
    or not bool(query_sgs_latest(int(params.get("code", 0)), int(params.get("n", 10)))),
)(_fetch_sgs_last_n)


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
    try:
        return fetch_sgs_series(code, use_cache=False)
    except RuntimeError:
        # Preserve the manual endpoint's last-good response; the failed live
        # attempt is already recorded and did not advance source freshness.
        return query_tr_periods() if code in MONTHLY_RATE_SERIES else query_sgs(code)
