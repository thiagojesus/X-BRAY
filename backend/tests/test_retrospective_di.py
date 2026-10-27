import io
import json
import sqlite3
import zipfile
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

import httpx
import pytest

from macro_score.retrospective_di import (
    _manifest_entries,
    _previous_request_monotonic,
    _retry_after_seconds,
    _safe_zip_file,
    backfill_cached_price_identities,
    collect_candidates,
    import_local_files,
    inventory,
    monthly_candidates,
    parse_local_files,
    revision_conflicts,
    validate_retrospective_target,
)

XML = b'''<?xml version="1.0" encoding="UTF-8"?>
<PriceReport xmlns="urn:bvmf.217.01.xsd"><PricRpt><TradDt><Dt>2026-10-19</Dt></TradDt>
<SctyId><TckrSymb>DI1X27</TckrSymb></SctyId><FinInstrmId><OthrId><Id>200001113994</Id></OthrId></FinInstrmId><FinInstrmAttrbts><AdjstdQtTax>12.980</AdjstdQtTax></FinInstrmAttrbts>
</PricRpt></PriceReport>'''


class FakeClock:
    def __init__(self) -> None:
        self.value = 0.0
        self.sleeps: list[float] = []

    def monotonic(self) -> float:
        return self.value

    def sleep(self, seconds: float) -> None:
        self.sleeps.append(seconds)
        self.value += seconds


def _zip_bytes(xml: bytes = XML) -> bytes:
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w") as output:
        output.writestr("BVBG.187.xml", xml)
    return buffer.getvalue()


def _response(url: str, status: int, content: bytes = b"", headers: dict[str, str] | None = None) -> httpx.Response:
    return httpx.Response(
        status, content=content, headers=headers,
        request=httpx.Request("GET", url),
    )


def test_parse_xml_import_isolated_idempotent_and_append_only(tmp_path: Path) -> None:
    first, revision = tmp_path / "first.xml", tmp_path / "revision.xml"
    first.write_bytes(XML)
    revision.write_bytes(XML.replace(b"12.980", b"12.981"))
    database = tmp_path / "isolated.db"
    fetched = datetime(2026, 10, 20, tzinfo=timezone.utc)
    assert len(parse_local_files([first])) == 1
    assert parse_local_files([first])[0]["security_id"] == "200001113994"
    assert import_local_files(database, [first], fetched_at=fetched)["inserted"] == 0
    result = import_local_files(database, [first], execute=True, fetched_at=fetched)
    assert result["inserted"] == 1
    assert import_local_files(database, [first], execute=True, fetched_at=fetched)["inserted"] == 0
    assert import_local_files(database, [revision], execute=True, fetched_at=fetched)["inserted"] == 1
    with sqlite3.connect(database) as db:
        rows = db.execute("SELECT rate,source_basis,source_hash FROM retrospective_di_observations ORDER BY rate").fetchall()
    assert [row[0] for row in rows] == [12.980, 12.981]
    assert {row[1] for row in rows} == {"retrospective_reference"}
    assert rows[0][2] != rows[1][2]


def test_cached_price_identity_backfill_is_readonly_by_default_and_append_only(tmp_path: Path) -> None:
    archive = tmp_path / "SPRD261019.zip"
    with zipfile.ZipFile(archive, "w") as output:
        output.writestr("BVBG.187.01.xml", XML)
    target = tmp_path / "isolated.db"
    assert backfill_cached_price_identities(target, [archive])["identities"] == 1
    assert not target.exists()
    first = backfill_cached_price_identities(target, [archive], execute=True)
    second = backfill_cached_price_identities(target, [archive], execute=True)
    assert first["inserted"] == 1 and second["inserted"] == 0
    with sqlite3.connect(target) as connection:
        identity = connection.execute(
            "SELECT trade_date,symbol,security_id,source_basis FROM retrospective_di_price_identities"
        ).fetchone()
    assert identity == ("2026-10-19", "DI1X27", "200001113994", "b3_price_identity_v1")


def test_zip_import_inventory_and_missing_months(tmp_path: Path) -> None:
    archive = tmp_path / "SPRD261019.zip"
    with zipfile.ZipFile(archive, "w") as output:
        output.writestr("BVBG.187.xml", XML)
    records = parse_local_files([archive])
    report = inventory(records, start=date(2026, 9, 1), end=date(2026, 10, 31))
    assert report["distinct_dates"] == 1
    assert report["months_with_data"] == 1
    assert report["missing_months"] == ["2026-09"]


def test_inventory_counts_months_not_trading_dates() -> None:
    dates = [f"2026-10-{day:02d}" for day in range(1, 25)]
    rows = [{"trade_date": day} for day in dates]
    report = inventory(rows, start=date(2026, 10, 1), end=date(2026, 10, 31))
    assert report["distinct_dates"] == 24
    assert report["months_with_data"] == 1


def test_plan_uses_last_complete_month_and_bounded_prior_sessions() -> None:
    planned = monthly_candidates(
        date(2026, 9, 1), date(2026, 10, 20), fallback_days=2,
    )
    dates = [date.fromisoformat(str(item["requested_date"])) for item in planned]
    assert {day.strftime("%Y-%m") for day in dates} == {"2026-09"}
    assert dates == sorted(dates)
    assert len(dates) == 3
    focus_candidates = monthly_candidates(
        date(2026, 10, 1), date(2026, 10, 20), include_current=True,
        focus_dates=[date(2026, 10, 16)],
    )
    assert any(
        item["requested_date"] == "2026-10-16" and item["reason"] == "focus_alignment_candidate"
        for item in focus_candidates
    )
    with pytest.raises(ValueError, match="date range"):
        monthly_candidates(date(2026, 11, 1), date(2026, 10, 1))
    with pytest.raises(ValueError, match="fallback"):
        monthly_candidates(date(2026, 9, 1), date(2026, 10, 1), fallback_days=11)


def test_retry_after_and_manifest_resume_are_bounded(tmp_path: Path) -> None:
    now = datetime(2026, 10, 20, tzinfo=timezone.utc)
    assert _retry_after_seconds(None, now) is None
    assert _retry_after_seconds("35", now) == 35
    assert _retry_after_seconds("not a date", now) is None
    assert _retry_after_seconds("Tue, 20 Oct 2026 00:00:00 GMT", now) == 0
    manifest = tmp_path / "manifest.jsonl"
    manifest.write_text("not json\n[]\n" + json.dumps({"requested_date": "2026-10-19", "status": "missing", "fetched_at": now.isoformat()}) + "\n", encoding="utf-8")
    entries = _manifest_entries(manifest)
    assert entries["2026-10-19"]["status"] == "missing"
    previous = _previous_request_monotonic(tmp_path, entries, monotonic=lambda: 100.0, now=lambda: now)
    assert previous == 100.0


def test_safe_archive_rejects_nonzip_empty_and_utf16_dtd(tmp_path: Path) -> None:
    invalid = tmp_path / "invalid.zip"
    invalid.write_bytes(b"not a zip")
    with pytest.raises(ValueError, match="not a ZIP"):
        _safe_zip_file(invalid)
    empty = tmp_path / "empty.zip"
    with zipfile.ZipFile(empty, "w") as output:
        output.writestr("empty.xml", b"<root/>")
    with pytest.raises(ValueError, match="no usable DI1"):
        _safe_zip_file(empty)
    utf16 = tmp_path / "utf16.xml"
    utf16.write_bytes('<!DOCTYPE x><x/>'.encode("utf-16"))
    with pytest.raises(ValueError, match="DTD/entity"):
        parse_local_files([utf16])


def test_collector_falls_back_after_missing_and_enforces_30_seconds(tmp_path: Path) -> None:
    clock = FakeClock()
    calls: list[str] = []

    def request(url: str, **_kwargs: object) -> httpx.Response:
        calls.append(url)
        if len(calls) == 1:
            return _response(url, 404)
        return _response(url, 200, _zip_bytes(), {"content-type": "application/zip"})

    planned = monthly_candidates(date(2026, 10, 1), date(2026, 10, 20), include_current=True, fallback_days=1)
    progress: list[dict[str, object]] = []
    result = collect_candidates(
        planned, cache_dir=tmp_path / "cache", database=tmp_path / "target.db",
        request=request, monotonic=clock.monotonic, sleep=clock.sleep,
        now=lambda: datetime(2026, 10, 20, tzinfo=timezone.utc),
        progress=progress.append,
    )
    assert len(calls) == 2
    assert clock.sleeps == [30.0]
    assert result["fetched_dates"] == ["2026-10-19"]
    assert len(result["missing"]) == 1
    assert result["inserted"] == 1
    assert result["requests_made"] == 2
    assert [event["status"] for event in progress] == ["missing", "downloaded"]
    manifest = (tmp_path / "cache" / "manifest.jsonl").read_text(encoding="utf-8")
    downloaded = next(json.loads(line) for line in manifest.splitlines() if '"status":"downloaded"' in line)
    assert downloaded["http_status"] == 200


def test_collector_treats_valid_empty_di_zip_as_candidate_gap(tmp_path: Path) -> None:
    clock = FakeClock()
    calls: list[str] = []

    def request(url: str, **_kwargs: object) -> httpx.Response:
        calls.append(url)
        body = _zip_bytes(b"<PriceReport/>") if len(calls) == 1 else _zip_bytes()
        return _response(url, 200, body, {"content-type": "application/zip"})

    result = collect_candidates(
        [
            {"requested_date": "2026-10-20", "reason": "month_end_candidate"},
            {"requested_date": "2026-10-19", "reason": "bounded_prior_trading_day"},
        ],
        cache_dir=tmp_path / "cache", database=tmp_path / "target.db",
        request=request, monotonic=clock.monotonic, sleep=clock.sleep,
    )
    assert len(calls) == 2
    assert clock.sleeps == [30.0]
    assert result["fetched_dates"] == ["2026-10-19"]
    assert result["missing"][0]["detail"] == "no_usable_di1_records"
    assert result["actual_trade_dates"] == ["2026-10-19"]


@pytest.mark.parametrize("status", [401, 403])
def test_collector_stops_on_auth_response(tmp_path: Path, status: int) -> None:
    calls: list[str] = []

    def request(url: str, **_kwargs: object) -> httpx.Response:
        calls.append(url)
        return _response(url, status)

    result = collect_candidates(
        [{"requested_date": "2026-10-19", "reason": "candidate"}],
        cache_dir=tmp_path / "cache", database=tmp_path / "target.db", request=request,
    )
    assert len(calls) == 1
    assert f"HTTP {status}" in str(result["halt_reason"])
    assert result["fetched_dates"] == []


def test_collector_bounds_429_retries_and_honors_retry_after_without_real_wait(tmp_path: Path) -> None:
    from ingestion_policy import RetryDeferred
    clock = FakeClock()
    calls: list[str] = []

    def request(url: str, **_kwargs: object) -> httpx.Response:
        calls.append(url)
        return _response(url, 429, headers={"retry-after": "30"})

    with pytest.raises(RetryDeferred) as deferred:
        collect_candidates(
            [{"requested_date": "2026-10-19", "reason": "candidate"}],
            cache_dir=tmp_path / "cache", database=tmp_path / "target.db",
            request=request, monotonic=clock.monotonic, sleep=clock.sleep,
        )
    assert len(calls) == 1
    assert clock.sleeps == []
    assert deferred.value.retry_not_before >= datetime.now(timezone.utc) + timedelta(seconds=29)


def test_collector_429_excessive_retry_after_stops_without_wait(tmp_path: Path) -> None:
    from ingestion_policy import RetryDeferred
    calls: list[str] = []
    clock = FakeClock()

    def request(url: str, **_kwargs: object) -> httpx.Response:
        calls.append(url)
        return _response(url, 429, headers={"retry-after": "301"})

    with pytest.raises(RetryDeferred) as deferred:
        collect_candidates(
            [{"requested_date": "2026-10-19", "reason": "candidate"}],
            cache_dir=tmp_path / "cache", database=tmp_path / "target.db",
            request=request, monotonic=clock.monotonic, sleep=clock.sleep,
        )
    assert len(calls) == 1
    assert clock.sleeps == []
    assert deferred.value.retry_not_before >= datetime.now(timezone.utc) + timedelta(seconds=300)


def test_collector_retries_server_error_and_timeout_with_bounded_spacing(tmp_path: Path) -> None:
    clock = FakeClock()
    calls: list[str] = []

    def request(url: str, **_kwargs: object) -> httpx.Response:
        calls.append(url)
        if len(calls) == 1:
            return _response(url, 503, headers={"retry-after": "45"})
        if len(calls) == 2:
            raise httpx.ReadTimeout("transient")
        return _response(url, 200, _zip_bytes(), {"content-type": "application/zip"})

    result = collect_candidates(
        [{"requested_date": "2026-10-19", "reason": "candidate"}],
        cache_dir=tmp_path / "cache", database=tmp_path / "target.db",
        request=request, monotonic=clock.monotonic, sleep=clock.sleep,
    )
    assert len(calls) == 3
    assert clock.sleeps == [45.0, 30.0]
    assert result["fetched_dates"] == ["2026-10-19"]


def test_collector_rejects_bad_archive_without_persisting(tmp_path: Path) -> None:
    def request(url: str, **_kwargs: object) -> httpx.Response:
        return _response(url, 200, b"not-a-zip")

    result = collect_candidates(
        [{"requested_date": "2026-10-19", "reason": "candidate"}],
        cache_dir=tmp_path / "cache", database=tmp_path / "target.db", request=request,
    )
    assert result["halt_reason"] is not None
    assert len(result["rejected"]) == 1
    assert not list((tmp_path / "cache").glob("SPRD*.zip"))


def test_collector_rejects_oversized_provider_payload(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("macro_score.retrospective_di.BINARY_MAX_BYTES", 1)

    def request(url: str, **_kwargs: object) -> httpx.Response:
        return _response(url, 200, b"not empty")

    result = collect_candidates(
        [{"requested_date": "2026-10-19", "reason": "candidate"}],
        cache_dir=tmp_path / "cache", database=tmp_path / "target.db", request=request,
    )
    assert "oversized provider payload" in str(result["halt_reason"])
    assert len(result["rejected"]) == 1


def test_collector_ignores_cache_hash_conflict_and_manifest_missing_is_resumable(tmp_path: Path) -> None:
    cache = tmp_path / "cache"
    cache.mkdir()
    with zipfile.ZipFile(cache / "SPRD261019.zip", "w") as output:
        output.writestr("BVBG.187.xml", XML)
    (cache / "manifest.jsonl").write_text(json.dumps({
        "requested_date": "2026-10-19", "status": "downloaded", "sha256": "wrong",
        "fetched_at": "2026-10-20T00:00:00+00:00",
    }) + "\n", encoding="utf-8")
    calls: list[str] = []

    def request(url: str, **_kwargs: object) -> httpx.Response:
        calls.append(url)
        return _response(url, 404)

    result = collect_candidates(
        [{"requested_date": "2026-10-19", "reason": "candidate"}],
        cache_dir=cache, database=tmp_path / "target.db", request=request,
    )
    assert calls == []
    assert result["rejected"]
    assert not (tmp_path / "target.db").exists()


def test_missing_manifest_resumes_without_repeating_provider_request(tmp_path: Path) -> None:
    cache = tmp_path / "cache"
    candidate = [{"requested_date": "2026-10-19", "reason": "candidate"}]
    calls: list[str] = []

    def missing(url: str, **_kwargs: object) -> httpx.Response:
        calls.append(url)
        return _response(url, 404)

    first = collect_candidates(
        candidate, cache_dir=cache, database=tmp_path / "target.db", request=missing,
    )
    progress: list[dict[str, object]] = []
    second = collect_candidates(
        candidate, cache_dir=cache, database=tmp_path / "target.db",
        request=missing, progress=progress.append,
    )
    assert first["requests_made"] == 1
    assert second["requests_made"] == 0
    assert len(calls) == 1
    assert progress == []


def test_collector_stops_on_challenge_and_reuses_cache_without_request(tmp_path: Path) -> None:
    calls: list[str] = []

    def challenge(url: str, **_kwargs: object) -> httpx.Response:
        calls.append(url)
        return _response(url, 200, b"<html>captcha</html>", {"content-type": "text/html"})

    result = collect_candidates(
        [{"requested_date": "2026-10-19", "reason": "candidate"}],
        cache_dir=tmp_path / "challenge", database=tmp_path / "challenge.db", request=challenge,
    )
    assert len(calls) == 1
    assert "challenge" in str(result["halt_reason"])

    cache = tmp_path / "cache"
    cache.mkdir()
    with zipfile.ZipFile(cache / "SPRD261019.zip", "w") as output:
        output.writestr("BVBG.187.xml", XML)
    result = collect_candidates(
        [{"requested_date": "2026-10-19", "reason": "candidate"}],
        cache_dir=cache, database=tmp_path / "cache.db", request=challenge,
    )
    assert len(calls) == 1
    assert result["cache_hit_dates"] == ["2026-10-19"]


def test_collector_rejects_too_fast_config_and_target_database(tmp_path: Path) -> None:
    with pytest.raises(ValueError, match="30 seconds"):
        collect_candidates(
            [], cache_dir=tmp_path / "cache", database=tmp_path / "target.db",
            min_interval_seconds=1,
        )
    with pytest.raises(ValueError, match="default database"):
        validate_retrospective_target(Path(__file__).parents[1] / "xbry.db")


def test_reject_dtd_and_unsafe_zip_paths(tmp_path: Path) -> None:
    xml = tmp_path / "bad.xml"
    xml.write_bytes(b'<!DOCTYPE x [<!ENTITY e "x">]><x/>')
    with pytest.raises(ValueError, match="DTD/entity"):
        parse_local_files([xml])
    archive = tmp_path / "unsafe.zip"
    with zipfile.ZipFile(archive, "w") as output:
        output.writestr("../outside.xml", XML)
    with pytest.raises(ValueError, match="unsafe"):
        parse_local_files([archive])


def test_target_guard_timezone_and_revision_conflict_is_not_guessed(tmp_path: Path) -> None:
    source = tmp_path / "x.xml"
    source.write_bytes(XML)
    with pytest.raises(ValueError, match="absolute"):
        import_local_files(Path("relative.db"), [source])
    with pytest.raises(ValueError, match="timezone-aware"):
        import_local_files(tmp_path / "local.db", [source], fetched_at=datetime(2026, 10, 20))  # noqa: DTZ001
    versions = [
        {"trade_date": "2026-10-19", "symbol": "DI1X27", "maturity": "2027-11-01", "rate": 12.98, "source_hash": "b"},
        {"trade_date": "2026-10-19", "symbol": "DI1X27", "maturity": "2027-11-01", "rate": 12.99, "source_hash": "a"},
    ]
    assert "chronology" in revision_conflicts(versions)[("2026-10-19", "DI1X27")]
