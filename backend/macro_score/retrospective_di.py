"""Isolated retrospective DI collection and local-file ingestion utilities."""

from __future__ import annotations

import hashlib
import io
import json
import sqlite3
import tempfile
import time
import xml.etree.ElementTree as ET
import zipfile
from collections.abc import Callable, Iterable, Mapping
from contextlib import closing
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import cast

import httpx

from datafetchers.b3_di import B3_SPR_URL, list_business_days, parse_di_xml
from provider_http import (
    BINARY_MAX_BYTES,
    ProviderPayloadTooLargeError,
    ProviderRequest,
    ProviderResponse,
    get_provider_response,
)

MAX_ARCHIVE_BYTES = 128 * 1024 * 1024
MAX_MEMBER_BYTES = 32 * 1024 * 1024
MAX_TOTAL_UNCOMPRESSED_BYTES = 128 * 1024 * 1024
MAX_MEMBERS = 500
MAX_COMPRESSION_RATIO = 200
SOURCE_BASIS = "retrospective_reference"
DEFAULT_MIN_REQUEST_INTERVAL = 30.0
MAX_RETRIES = 2
MAX_RETRY_AFTER = 300.0
REQUEST_TIMEOUT = 30.0
DEFAULT_FALLBACK_DAYS = 3


@dataclass(slots=True)
class _CollectorState:
    last_request: float | None
    fetched: list[str] = field(default_factory=list)
    cached: list[str] = field(default_factory=list)
    missing: list[dict[str, object]] = field(default_factory=list)
    rejected: list[dict[str, object]] = field(default_factory=list)
    halt_reason: str | None = None
    requests_made: int = 0
    satisfied_months: set[str] = field(default_factory=set)
    processed_dates: set[str] = field(default_factory=set)


class NoDIObservationsError(ValueError):
    """A valid bulletin archive had no usable DI observations for the candidate day."""


def monthly_candidates(
    start: date, end: date, *, include_current: bool = False,
    fallback_days: int = DEFAULT_FALLBACK_DAYS,
    focus_dates: Iterable[date] = (),
) -> list[dict[str, object]]:
    if start > end or not 0 <= fallback_days <= 10:
        raise ValueError("invalid retrospective DI date range or fallback count")
    last_month = date(end.year, end.month, 1)
    if not include_current:
        last_month = date(end.year, end.month, 1) - timedelta(days=1)
        last_month = date(last_month.year, last_month.month, 1)
    cursor = date(start.year, start.month, 1)
    candidates: dict[date, set[str]] = {}
    while cursor <= last_month:
        month_end = (date(cursor.year + (cursor.month == 12), cursor.month % 12 + 1, 1)
                     - timedelta(days=1))
        candidate_end = min(month_end, end)
        if start <= candidate_end:
            business_days = [day for day in list_business_days(candidate_end, fallback_days + 1)
                             if day >= start]
            for index, day in enumerate(reversed(business_days)):
                reason = "month_end_candidate" if index == 0 else "bounded_prior_trading_day"
                candidates.setdefault(day, set()).add(reason)
        cursor = date(cursor.year + (cursor.month == 12), cursor.month % 12 + 1, 1)
    for day in focus_dates:
        if start <= day <= end:
            preceding = list_business_days(day, 1)
            aligned = preceding[-1] if preceding else day
            if start <= aligned <= end:
                candidates.setdefault(aligned, set()).add("focus_alignment_candidate")
    return [
        {"requested_date": day.isoformat(), "reason": reason}
        for day, reasons in sorted(candidates.items())
        for reason in sorted(reasons)
    ]


def _retry_after_seconds(value: str | None, now: datetime) -> float | None:
    if not value:
        return None
    try:
        return max(0.0, float(value))
    except ValueError:
        try:
            from email.utils import parsedate_to_datetime

            target = parsedate_to_datetime(value)
            if target.tzinfo is None:
                target = target.replace(tzinfo=timezone.utc)
            return max(0.0, (target - now).total_seconds())
        except (TypeError, ValueError, OverflowError):
            return None


def _response_from_error(error: httpx.HTTPStatusError) -> httpx.Response | None:
    return error.response


def validate_retrospective_target(target: Path) -> None:
    if not target.is_absolute() or target.suffix != ".db":
        raise ValueError("an absolute local .db target path is required")
    default_db = Path(__file__).resolve().parents[1] / "xbry.db"
    if target.resolve() == default_db.resolve():
        raise ValueError("refusing to write to the application's default database")


def _append_manifest(path: Path, entry: Mapping[str, object]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(entry, sort_keys=True, separators=(",", ":")) + "\n")
        handle.flush()


def _manifest_entries(path: Path) -> dict[str, dict[str, object]]:
    entries: dict[str, dict[str, object]] = {}
    if not path.exists():
        return entries
    for line in path.read_text(encoding="utf-8").splitlines():
        try:
            row = json.loads(line)
        except json.JSONDecodeError:
            continue
        if isinstance(row, dict):
            record = cast(dict[str, object], row)
            requested = record.get("requested_date")
            if isinstance(requested, str):
                entries[requested] = record
    return entries


def _previous_request_monotonic(
    cache: Path, rows: Mapping[str, dict[str, object]], *,
    monotonic: Callable[[], float], now: Callable[[], datetime],
) -> float | None:
    timestamps: list[datetime] = []
    for row in rows.values():
        if row.get("status") not in {"downloaded", "missing", "stopped", "rejected"}:
            continue
        value = row.get("fetched_at")
        if isinstance(value, str):
            try:
                parsed = datetime.fromisoformat(value)
            except ValueError:
                continue
            if parsed.tzinfo is not None:
                timestamps.append(parsed.astimezone(timezone.utc))
    for archive in cache.glob("SPRD*.zip"):
        timestamps.append(datetime.fromtimestamp(archive.stat().st_mtime, timezone.utc))
    if not timestamps:
        return None
    elapsed = max(0.0, (now().astimezone(timezone.utc) - max(timestamps)).total_seconds())
    return monotonic() - elapsed


def _safe_zip_file(path: Path) -> tuple[list[dict[str, object]], str]:
    if path.stat().st_size > MAX_ARCHIVE_BYTES:
        raise ValueError("archive exceeds compressed size limit")
    if not zipfile.is_zipfile(path):
        raise ValueError("B3 response is not a ZIP archive")
    digest = hashlib.sha256(path.read_bytes()).hexdigest()
    records = parse_local_files([path])
    if not records:
        raise NoDIObservationsError("B3 archive contains no usable DI1 AdjstdQtTax records")
    return records, digest


def _is_challenge(content_type: str, content: bytes) -> bool:
    prefix = content[:8192].lower()
    return ("text/html" in content_type.lower() or b"<html" in prefix
            or b"captcha" in prefix or b"access denied" in prefix
            or b"challenge-platform" in prefix)


def _handle_http_status(
    error: httpx.HTTPStatusError, requested: str, reason: str,
    fetched_at: datetime, attempt: int, interval: float,
    sleep: Callable[[float], None],
) -> tuple[str, dict[str, object], str | None]:
    response = _response_from_error(error)
    status = response.status_code if response else None
    retry_after = _retry_after_seconds(
        response.headers.get("retry-after") if response else None, fetched_at
    )
    if status == 404:
        entry = _manifest_row(
            requested, reason, fetched_at, "missing", http_status=404,
            detail="bulletin_not_found", attempts=attempt + 1,
        )
        return "missing", entry, None
    if status in {401, 403}:
        halt = f"provider authorization/access response HTTP {status}; stopped"
    elif status == 429:
        if attempt >= MAX_RETRIES:
            halt = "persistent HTTP 429; stopped after bounded retries"
        elif retry_after is not None and retry_after > MAX_RETRY_AFTER:
            halt = "Retry-After exceeds bounded wait; stopped without retry"
        else:
            sleep(max(retry_after or 0.0, interval, 2 ** attempt))
            return "retry", {}, None
    elif status is not None and 500 <= status < 600 and attempt < MAX_RETRIES:
        if retry_after is not None and retry_after > MAX_RETRY_AFTER:
            halt = "Retry-After exceeds bounded wait; stopped without retry"
        else:
            sleep(max(retry_after or 0.0, interval, 2 ** attempt))
            return "retry", {}, None
    else:
        halt = f"provider HTTP {status or 'error'}; stopped"
    entry = _manifest_row(
        requested, reason, fetched_at, "stopped", http_status=status,
        detail=halt, attempts=attempt + 1,
    )
    return "stopped", entry, halt


def _store_response(
    response: ProviderResponse, requested: str, reason: str, archive: Path,
    fetched_at: datetime, attempts: int,
) -> tuple[str, dict[str, object], str | None]:
    content = response.content
    content_type = response.headers.get("content-type", "")
    if _is_challenge(content_type, content):
        halt = "HTML/captcha/access challenge; stopped without bypass"
        entry = _manifest_row(
            requested, reason, fetched_at, "stopped", detail=halt,
            attempts=attempts, http_status=response.status_code,
            content_type=content_type,
        )
        return "stopped", entry, halt
    if not content:
        entry = _manifest_row(
            requested, reason, fetched_at, "missing", detail="empty_response",
            attempts=attempts, http_status=response.status_code,
        )
        return "missing", entry, None
    return _store_zip_content(
        content, requested, reason, archive, fetched_at, attempts,
        response.status_code, content_type,
    )


def _store_zip_content(
    content: bytes, requested: str, reason: str, archive: Path,
    fetched_at: datetime, attempts: int, status_code: int, content_type: str,
) -> tuple[str, dict[str, object], str | None]:
    temporary: Path | None = None
    try:
        with tempfile.NamedTemporaryFile(dir=archive.parent, suffix=".zip", delete=False) as handle:
            handle.write(content)
            temporary = Path(handle.name)
        records, digest = _safe_zip_file(temporary)
        temporary.replace(archive)
    except NoDIObservationsError:
        if temporary:
            temporary.unlink(missing_ok=True)
        entry = _manifest_row(
            requested, reason, fetched_at, "missing", detail="no_usable_di1_records",
            attempts=attempts, http_status=status_code, payload_bytes=len(content),
        )
        return "missing", entry, None
    except (OSError, ValueError, zipfile.BadZipFile) as error:
        if temporary:
            temporary.unlink(missing_ok=True)
        halt = f"invalid archive response; stopped: {error}"
        entry = _manifest_row(
            requested, reason, fetched_at, "rejected", detail=str(error),
            attempts=attempts, http_status=status_code, payload_bytes=len(content),
        )
        return "rejected", entry, halt
    actual_dates = sorted({str(row["trade_date"]) for row in records})
    entry = _manifest_row(
        requested, reason, fetched_at, "downloaded", sha256=digest,
        actual_trade_dates=actual_dates, attempts=attempts,
        http_status=status_code, content_type=content_type,
        payload_bytes=len(content),
    )
    return "downloaded", entry, None


def _request_archive(
    requested: date, reason: str, archive: Path, manifest: Path, *,
    request_fn: ProviderRequest, interval: float, last_request: float | None,
    monotonic: Callable[[], float], sleep: Callable[[float], None],
    now: Callable[[], datetime],
) -> tuple[str, dict[str, object], float | None, str | None]:
    attempts = 0
    requested_text = requested.isoformat()
    url = B3_SPR_URL.format(YYMMDD=requested.strftime("%y%m%d"))
    for attempt in range(MAX_RETRIES + 1):
        if last_request is not None:
            wait = interval - (monotonic() - last_request)
            if wait > 0:
                sleep(wait)
        fetched_at = now().astimezone(timezone.utc)
        last_request = monotonic()
        attempts += 1
        try:
            response = get_provider_response(
                url, request=request_fn, timeout=REQUEST_TIMEOUT,
                max_bytes=BINARY_MAX_BYTES, follow_redirects=True,
                headers={"Accept": "application/zip"},
            )
        except httpx.HTTPStatusError as error:
            outcome, entry, halt = _handle_http_status(
                error, requested_text, reason, fetched_at, attempt,
                interval, sleep,
            )
            if outcome == "retry":
                continue
            return outcome, entry, last_request, halt
        except httpx.RequestError as error:
            if attempt < MAX_RETRIES:
                sleep(max(interval, 2 ** attempt))
                continue
            halt = f"bounded request retries exhausted: {type(error).__name__}"
            return "stopped", _manifest_row(
                requested_text, reason, fetched_at, "stopped", detail=halt,
                attempts=attempts,
            ), last_request, halt
        except ProviderPayloadTooLargeError as error:
            halt = f"oversized provider payload; stopped: {error}"
            return "rejected", _manifest_row(
                requested_text, reason, fetched_at, "rejected", detail=halt,
                attempts=attempts,
            ), last_request, halt
        outcome, entry, halt = _store_response(
            response, requested_text, reason, archive, fetched_at, attempts,
        )
        return outcome, entry, last_request, halt
    raise RuntimeError("unreachable B3 request retry state")


def _manifest_row(
    requested: str, reason: str, fetched_at: datetime, status: str, **metadata: object,
) -> dict[str, object]:
    return {
        "requested_date": requested, "actual_trade_dates": [],
        "fetched_at": fetched_at.isoformat(), "status": status,
        "candidate_reason": reason, "source_basis": SOURCE_BASIS, **metadata,
    }


def _import_cache(cache: Path, database: Path, manifest_rows: dict[str, dict[str, object]]) -> tuple[list[dict[str, object]], int]:
    records: list[dict[str, object]] = []
    inserted = 0
    for archive in sorted(cache.glob("SPRD*.zip")):
        try:
            parsed, digest = _safe_zip_file(archive)
            requested = datetime.strptime(
                archive.stem.removeprefix("SPRD"), "%y%m%d"
            ).replace(tzinfo=timezone.utc).date().isoformat()
            declared_hash = manifest_rows.get(requested, {}).get("sha256")
            if isinstance(declared_hash, str) and declared_hash != digest:
                continue
            timestamp = str(manifest_rows.get(requested, {}).get("fetched_at")
                            or datetime.fromtimestamp(archive.stat().st_mtime, timezone.utc).isoformat())
            result = import_local_files(
                database, [archive], execute=True, fetched_at=datetime.fromisoformat(timestamp),
            )
        except (OSError, ValueError, zipfile.BadZipFile):
            continue
        records.extend(parsed)
        value = result["inserted"]
        inserted += int(value) if isinstance(value, int) else 0
    return records, inserted


def _collect_candidate(
    requested: date, reason: str, *, cache: Path, manifest: Path,
    manifest_rows: dict[str, dict[str, object]], request_fn: ProviderRequest,
    interval: float, last_request: float | None,
    monotonic: Callable[[], float], sleep: Callable[[float], None],
    now: Callable[[], datetime],
) -> tuple[str, dict[str, object], float | None, str | None]:
    requested_text = requested.isoformat()
    archive = cache / f"SPRD{requested.strftime('%y%m%d')}.zip"
    previous = manifest_rows.get(requested_text, {})
    if previous.get("status") == "missing" and not archive.exists():
        return "known_missing", previous, last_request, None
    if archive.exists():
        try:
            records, digest = _safe_zip_file(archive)
        except (OSError, ValueError, zipfile.BadZipFile) as error:
            return "rejected", {"requested_date": requested_text, "reason": str(error)}, last_request, None
        if str(previous.get("sha256", digest)) != digest:
            return "rejected", {"requested_date": requested_text, "reason": "cache hash differs from manifest"}, last_request, None
        actual_dates = sorted({str(row["trade_date"]) for row in records})
        fetched_at = str(previous.get("fetched_at") or datetime.fromtimestamp(archive.stat().st_mtime, timezone.utc).isoformat())
        entry: dict[str, object] = {
            "requested_date": requested_text, "actual_trade_dates": actual_dates,
            "fetched_at": fetched_at, "status": "cache_hit", "sha256": digest,
            "reason": reason, "source_basis": SOURCE_BASIS,
        }
        _append_manifest(manifest, entry)
        manifest_rows[requested_text] = entry
        return "cache_hit", entry, last_request, None
    status, entry, last_request, halt = _request_archive(
        requested, reason, archive, manifest, request_fn=request_fn,
        interval=interval, last_request=last_request,
        monotonic=monotonic, sleep=sleep, now=now,
    )
    _append_manifest(manifest, entry)
    manifest_rows[requested_text] = entry
    return status, entry, last_request, halt


def _group_candidates(
    candidates: Iterable[dict[str, object]],
) -> list[tuple[tuple[str, bool], list[tuple[date, str]]]]:
    grouped: dict[tuple[str, bool], list[tuple[date, str]]] = {}
    for item in candidates:
        requested = date.fromisoformat(str(item["requested_date"]))
        reason = str(item.get("reason", "planned"))
        focus = reason == "focus_alignment_candidate"
        grouped.setdefault((requested.strftime("%Y-%m"), focus), []).append((requested, reason))
    return sorted(grouped.items(), key=lambda item: (item[0][0], item[0][1]))


def _record_candidate_result(
    state: _CollectorState, *, status: str, entry: dict[str, object],
    requested: str, month: str, focus: bool,
    progress: Callable[[dict[str, object]], None] | None,
) -> None:
    attempts = entry.get("attempts", 0)
    if isinstance(attempts, int) and status != "known_missing":
        state.requests_made += attempts
    if progress is not None and status not in {"cache_hit", "known_missing"}:
        progress({"requested_date": requested, "status": status,
                  "attempts": attempts, "requests_made": state.requests_made})
    if status == "cache_hit":
        state.cached.append(requested)
    elif status == "downloaded":
        state.fetched.append(requested)
    elif status in {"missing", "known_missing"}:
        state.missing.append(entry)
    elif status == "rejected":
        state.rejected.append(entry)
    if status in {"cache_hit", "downloaded"} and not focus:
        state.satisfied_months.add(month)


def _process_candidate_group(
    month: str, focus: bool, group: list[tuple[date, str]], *,
    state: _CollectorState, processed: set[str], cache: Path,
    manifest: Path, manifest_rows: dict[str, dict[str, object]],
    request_fn: ProviderRequest, interval: float,
    monotonic: Callable[[], float], sleep: Callable[[float], None],
    now: Callable[[], datetime],
    progress: Callable[[dict[str, object]], None] | None,
) -> None:
    if not focus and month in state.satisfied_months:
        return
    for requested_day, reason in sorted(set(group), key=lambda item: item[0], reverse=True):
        requested = requested_day.isoformat()
        if requested in processed:
            continue
        processed.add(requested)
        if state.halt_reason:
            break
        status, entry, state.last_request, state.halt_reason = _collect_candidate(
            requested_day, reason, cache=cache, manifest=manifest,
            manifest_rows=manifest_rows, request_fn=request_fn,
            interval=interval, last_request=state.last_request,
            monotonic=monotonic, sleep=sleep, now=now,
        )
        _record_candidate_result(
            state, status=status, entry=entry, requested=requested,
            month=month, focus=focus, progress=progress,
        )
        if status in {"cache_hit", "downloaded"} and not focus:
            break
        if state.halt_reason:
            break


def _run_candidate_groups(
    candidates: Iterable[dict[str, object]], *, state: _CollectorState,
    cache: Path, manifest: Path,
    manifest_rows: dict[str, dict[str, object]], request_fn: ProviderRequest,
    interval: float, monotonic: Callable[[], float],
    sleep: Callable[[float], None], now: Callable[[], datetime],
    progress: Callable[[dict[str, object]], None] | None,
) -> None:
    processed: set[str] = set()
    for (month, focus), group in _group_candidates(candidates):
        _process_candidate_group(
            month, focus, group, state=state, processed=processed,
            cache=cache, manifest=manifest, manifest_rows=manifest_rows,
            request_fn=request_fn, interval=interval, monotonic=monotonic,
            sleep=sleep, now=now, progress=progress,
        )
        if state.halt_reason:
            break


def collect_candidates(
    candidates: Iterable[dict[str, object]], *, cache_dir: Path,
    database: Path,
    min_interval_seconds: float = DEFAULT_MIN_REQUEST_INTERVAL,
    fallback_days: int = DEFAULT_FALLBACK_DAYS,
    request: ProviderRequest | None = None,
    progress: Callable[[dict[str, object]], None] | None = None,
    monotonic: Callable[[], float] = time.monotonic,
    sleep: Callable[[float], None] = time.sleep,
    now: Callable[[], datetime] = lambda: datetime.now(timezone.utc),
) -> dict[str, object]:
    if min_interval_seconds < DEFAULT_MIN_REQUEST_INTERVAL:
        raise ValueError("minimum request interval may not be lower than 30 seconds")
    if not 0 <= fallback_days <= 10:
        raise ValueError("fallback_days must be between 0 and 10")
    target = Path(database)
    validate_retrospective_target(target)
    cache = Path(cache_dir)
    cache.mkdir(parents=True, exist_ok=True)
    manifest = cache / "manifest.jsonl"
    manifest_rows = _manifest_entries(manifest)
    state = _CollectorState(_previous_request_monotonic(
        cache, manifest_rows, monotonic=monotonic, now=now,
    ))
    request_fn = request or httpx.get
    _run_candidate_groups(
        candidates, state=state, cache=cache, manifest=manifest,
        manifest_rows=manifest_rows, request_fn=request_fn,
        interval=min_interval_seconds, monotonic=monotonic, sleep=sleep,
        now=now, progress=progress,
    )
    all_records, inserted = _import_cache(cache, target, manifest_rows)
    conflicts = revision_conflicts(all_records)
    return {
        "fetched_dates": state.fetched, "cache_hit_dates": state.cached,
        "missing": state.missing, "rejected": state.rejected,
        "halt_reason": state.halt_reason, "requests_made": state.requests_made,
        "records": len(all_records),
        "inserted": inserted,
        "actual_trade_dates": sorted({str(row["trade_date"]) for row in all_records}),
        "revision_conflicts": [
            {"trade_date": key[0], "symbol": key[1], "reason": detail}
            for key, detail in sorted(conflicts.items())
        ],
        "manifest": str(manifest), "cache_dir": str(cache),
    }


def _validate_zip_member(item: zipfile.ZipInfo) -> None:
    unsafe_path = item.filename.startswith(("/", "\\")) or ".." in Path(item.filename).parts
    too_large = item.file_size > MAX_MEMBER_BYTES
    bad_ratio = item.compress_size == 0 and item.file_size > 0
    bad_ratio = bad_ratio or bool(
        item.compress_size and item.file_size / item.compress_size > MAX_COMPRESSION_RATIO
    )
    if unsafe_path:
        raise ValueError("unsafe archive member path")
    if too_large or bad_ratio:
        raise ValueError("archive member exceeds retrospective DI safety limits")


def _zip_member_xml(
    archive: zipfile.ZipFile, item: zipfile.ZipInfo, name: str, depth: int,
) -> Iterable[tuple[str, bytes]]:
    if item.is_dir():
        return
    _validate_zip_member(item)
    raw = archive.read(item)
    member_name = f"{name}!{item.filename}"
    if zipfile.is_zipfile(io.BytesIO(raw)):
        yield from _xml_members(raw, member_name, depth + 1)
    elif item.filename.lower().endswith(".xml"):
        yield member_name, raw


def _xml_members(data: bytes, name: str, depth: int = 0) -> Iterable[tuple[str, bytes]]:
    if len(data) > MAX_ARCHIVE_BYTES or depth > 2:
        raise ValueError("archive exceeds retrospective DI safety limits")
    if not zipfile.is_zipfile(io.BytesIO(data)):
        if not name.lower().endswith(".xml"):
            raise ValueError(f"unsupported local DI input: {name}")
        yield name, data
        return
    with zipfile.ZipFile(io.BytesIO(data)) as archive:
        members = archive.infolist()
        if len(members) > MAX_MEMBERS:
            raise ValueError("archive has too many members")
        if sum(item.file_size for item in members) > MAX_TOTAL_UNCOMPRESSED_BYTES:
            raise ValueError("archive exceeds total uncompressed size limit")
        for item in members:
            yield from _zip_member_xml(archive, item, name, depth)


def _has_xml_declarations(data: bytes) -> bool:
    if b"<!DOCTYPE" in data.upper() or b"<!ENTITY" in data.upper():
        return True
    for encoding in ("utf-16", "utf-32"):
        try:
            decoded = data.decode(encoding).upper()
        except UnicodeError:
            continue
        if "<!DOCTYPE" in decoded or "<!ENTITY" in decoded:
            return True
    return False


def parse_local_files(paths: Iterable[Path]) -> list[dict[str, object]]:
    """Parse XML/ZIP files without persistence or access to the live B3 table."""
    records: list[dict[str, object]] = []
    for path in sorted((Path(value) for value in paths), key=lambda item: str(item)):
        raw = path.read_bytes()
        if len(raw) > MAX_ARCHIVE_BYTES:
            raise ValueError(f"local input exceeds size limit: {path.name}")
        for member, xml in _xml_members(raw, path.name):
            if _has_xml_declarations(xml):
                raise ValueError(f"DTD/entity declarations are not accepted: {member}")
            try:
                ET.fromstring(xml)
            except ET.ParseError as error:
                raise ValueError(f"malformed XML source member: {member}") from error
            file_hash = hashlib.sha256(xml).hexdigest()
            for row in parse_di_xml(xml):
                trade_date = date.fromisoformat(str(row["trade_date"])).isoformat()
                rate = row.get("rate")
                if isinstance(rate, bool) or not isinstance(rate, (int, float)):
                    continue
                records.append({
                    "trade_date": trade_date, "symbol": str(row["symbol"]),
                    "maturity": str(row["maturity"]), "rate": float(rate),
                    "source_hash": file_hash, "revision": f"sha256:{file_hash}",
                    "source_name": path.name, "source_member": member,
                })
    return sorted(records, key=lambda row: (
        str(row["trade_date"]), str(row["symbol"]), str(row["source_hash"]),
        str(row["source_member"]),
    ))


def inventory(records: list[dict[str, object]], *, start: date, end: date) -> dict[str, object]:
    if start > end:
        raise ValueError("inventory date range must be ordered")
    months: dict[str, set[str]] = {}
    dates: set[str] = set()
    for row in records:
        day = str(row["trade_date"])
        parsed = date.fromisoformat(day)
        if start <= parsed <= end:
            dates.add(day)
            months.setdefault(day[:7], set()).add(day)
    cursor = date(start.year, start.month, 1)
    end_month = date(end.year, end.month, 1)
    missing: list[str] = []
    while cursor <= end_month:
        if cursor.strftime("%Y-%m") not in months:
            missing.append(cursor.strftime("%Y-%m"))
        cursor = date(cursor.year + (cursor.month == 12), cursor.month % 12 + 1, 1)
    return {
        "start": start.isoformat(), "end": end.isoformat(),
        "record_count": sum(start <= date.fromisoformat(str(r["trade_date"])) <= end for r in records),
        "distinct_dates": len(dates), "months_with_data": len(months),
        "dates": sorted(dates), "missing_months": missing,
        "monthly_dates": {key: sorted(value) for key, value in sorted(months.items())},
    }


def import_local_files(
    database: Path, paths: Iterable[Path], *, execute: bool = False,
    fetched_at: datetime | None = None,
) -> dict[str, object]:
    """Inventory by default; explicit execution writes only an isolated SQLite file."""
    target = Path(database)
    validate_retrospective_target(target)
    inputs = tuple(Path(path) for path in paths)
    rows = parse_local_files(inputs)
    fetched = fetched_at or datetime.now(timezone.utc)
    if fetched.tzinfo is None or fetched.utcoffset() is None:
        raise ValueError("fetched_at must be timezone-aware")
    fetched_text = fetched.astimezone(timezone.utc).isoformat()
    result: dict[str, object] = {
        "dry_run": not execute, "source_basis": SOURCE_BASIS,
        "fetched_at": fetched_text, "files": [str(path) for path in inputs],
        "records": len(rows), "distinct_dates": len({str(row["trade_date"]) for row in rows}),
        "months": sorted({str(row["trade_date"])[:7] for row in rows}), "inserted": 0,
    }
    if execute:
        target.parent.mkdir(parents=True, exist_ok=True)
        with closing(sqlite3.connect(target)) as connection:
            connection.execute("""CREATE TABLE IF NOT EXISTS retrospective_di_observations (
                trade_date TEXT NOT NULL, symbol TEXT NOT NULL, maturity TEXT NOT NULL,
                rate REAL NOT NULL, source_hash TEXT NOT NULL, fetched_at TEXT NOT NULL,
                revision TEXT NOT NULL, source_basis TEXT NOT NULL, source_name TEXT NOT NULL,
                source_member TEXT NOT NULL,
                maturity_basis TEXT NOT NULL DEFAULT 'legacy_local_unverified',
                PRIMARY KEY (trade_date, symbol, source_hash, source_member))""")
            columns = {
                str(row[1]) for row in connection.execute(
                    "PRAGMA table_info(retrospective_di_observations)"
                ).fetchall()
            }
            if "maturity_basis" not in columns:
                connection.execute(
                    "ALTER TABLE retrospective_di_observations ADD COLUMN "
                    "maturity_basis TEXT NOT NULL DEFAULT 'legacy_local_unverified'"
                )
            before = connection.total_changes
            connection.executemany("""INSERT OR IGNORE INTO retrospective_di_observations
                (trade_date,symbol,maturity,rate,source_hash,fetched_at,revision,source_basis,source_name,source_member,maturity_basis)
                VALUES (?,?,?,?,?,?,?,?,?,?,?)""", [(
                    row["trade_date"], row["symbol"], row["maturity"], row["rate"],
                    row["source_hash"], fetched_text, row["revision"], SOURCE_BASIS,
                    row["source_name"], row["source_member"],
                    str(row.get("maturity_basis", "legacy_local_unverified")),
                ) for row in rows])
            result["inserted"] = connection.total_changes - before
            result["stored_revisions"] = connection.execute(
                "SELECT COUNT(DISTINCT source_hash) FROM retrospective_di_observations"
            ).fetchone()[0]
            connection.commit()
    return result


def revision_conflicts(rows: Iterable[dict[str, object]]) -> dict[tuple[str, str], str]:
    """Identify unresolved differing revisions; never infer chronology from a hash/name."""
    versions: dict[tuple[str, str], set[tuple[str, str, str]]] = {}
    for row in rows:
        key = (str(row["trade_date"]), str(row["symbol"]))
        value = (str(row["maturity"]), str(row["rate"]), str(row["source_hash"]))
        versions.setdefault(key, set()).add(value)
    conflicts: dict[tuple[str, str], str] = {}
    for key, candidates in versions.items():
        values = {(maturity, rate) for maturity, rate, _digest in candidates}
        if len(values) > 1:
            conflicts[key] = "conflicting revisions; chronology/version metadata required"
    return conflicts
