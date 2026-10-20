import argparse
import json
import sqlite3
import sys
import zipfile
from contextlib import closing
from datetime import date, datetime, timezone
from math import isfinite
from pathlib import Path
from typing import cast

from macro_score.retrospective_di import (
    DEFAULT_FALLBACK_DAYS,
    DEFAULT_MIN_REQUEST_INTERVAL,
    collect_candidates,
    import_local_files,
    inventory,
    monthly_candidates,
    parse_local_files,
    validate_retrospective_target,
)


def _month_start(value: date, offset: int) -> date:
    index = value.year * 12 + value.month - 1 + offset
    year, month = divmod(index, 12)
    return date(year, month + 1, 1)


def _target_rows(path: Path) -> list[dict[str, object]]:
    if not path.exists():
        return []
    try:
        with closing(sqlite3.connect(f"{path.as_uri()}?mode=ro", uri=True)) as connection:
            tables = {
                row[0] for row in connection.execute(
                    "SELECT name FROM sqlite_master WHERE type='table'"
                )
            }
            rows: list[dict[str, object]] = []
            if "retrospective_di_observations" in tables:
                rows.extend({
                    "trade_date": row[0], "symbol": row[1], "maturity": row[2],
                    "rate": row[3], "source_hash": row[4],
                } for row in connection.execute(
                    "SELECT trade_date,symbol,maturity,rate,source_hash FROM retrospective_di_observations"
                ))
            if "b3_di" in tables:
                rows.extend({
                    "trade_date": row[0], "symbol": row[1], "maturity": row[2],
                    "rate": row[3], "source_hash": "current-b3-di",
                } for row in connection.execute(
                    "SELECT trade_date,symbol,maturity,rate FROM b3_di WHERE rate IS NOT NULL"
                ))
            if "observation_vintages" in tables:
                for reference, dimensions_json, value_json in connection.execute(
                    "SELECT reference_period,dimensions_json,raw_value_json FROM observation_vintages "
                    "WHERE provider='b3' AND series_key='di1'"
                ):
                    try:
                        dimension_value: object = json.loads(dimensions_json)
                        raw_value: object = json.loads(value_json)
                    except (TypeError, ValueError):
                        continue
                    if not isinstance(dimension_value, dict) or not isinstance(raw_value, dict):
                        continue
                    dimensions = cast(dict[str, object], dimension_value)
                    raw = cast(dict[str, object], raw_value)
                    rate = raw.get("rate")
                    if not isinstance(rate, (int, float)) or isinstance(rate, bool) or not isfinite(float(rate)):
                        continue
                    rows.append({
                        "trade_date": reference, "symbol": dimensions.get("symbol"),
                        "maturity": dimensions.get("maturity"), "rate": rate,
                        "source_hash": "b3-di-vintage",
                    })
            return rows
    except sqlite3.Error as error:
        raise ValueError(f"could not inspect isolated SQLite target: {path}") from error


def _input_paths(items: list[Path]) -> list[Path]:
    return sorted({
        path for item in items
        for path in (item.rglob("*") if item.is_dir() else (item,))
        if path.is_file() and path.suffix.lower() in {".xml", ".zip"}
    })


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Plan or collect retrospective DI bulletins from the existing B3 endpoint"
    )
    parser.add_argument("--sqlite", required=True, type=Path, help="explicit isolated SQLite target (.db)")
    parser.add_argument("--cache-dir", type=Path, default=Path(__file__).resolve().parents[1] / "cache/retrospective_di")
    parser.add_argument("--start", type=date.fromisoformat)
    parser.add_argument("--months", type=int, default=36, help="bounded planning window (1..120 months)")
    parser.add_argument("--include-current", action="store_true", help="include current month candidate")
    parser.add_argument("--fallback-days", type=int, default=DEFAULT_FALLBACK_DAYS, help="prior trading-day alternatives (0..10)")
    parser.add_argument("--focus-date", action="append", type=date.fromisoformat, default=[], help="extra locally known Focus-aligned DI date")
    parser.add_argument("--input", action="append", type=Path, default=[], help="local XML/ZIP files or directories to reuse")
    parser.add_argument("--min-request-interval", type=float, default=DEFAULT_MIN_REQUEST_INTERVAL)
    parser.add_argument("--execute", action="store_true", help="allow B3 requests and isolated ledger writes")
    return parser


def _parse_args() -> argparse.Namespace:
    parser = _parser()
    today = datetime.now(timezone.utc).date()
    parser.add_argument("--end", type=date.fromisoformat, default=today)
    args = parser.parse_args()
    try:
        validate_retrospective_target(args.sqlite)
    except ValueError as error:
        parser.error(str(error))
    if not 1 <= args.months <= 120:
        parser.error("--months must be between 1 and 120")
    if args.end > today:
        parser.error("--end may not be in the future")
    if args.min_request_interval < DEFAULT_MIN_REQUEST_INTERVAL:
        parser.error("--min-request-interval must be at least 30 seconds")
    return args


def _source_inventory(
    paths: list[Path], cache_dir: Path, target: Path,
) -> tuple[list[dict[str, object]], list[dict[str, str]]]:
    records = parse_local_files(paths)
    rejected: list[dict[str, str]] = []
    cache_files = sorted(cache_dir.glob("SPRD*.zip")) if cache_dir.exists() else []
    for cache_file in cache_files:
        try:
            records.extend(parse_local_files([cache_file]))
        except (OSError, ValueError, zipfile.BadZipFile) as error:
            rejected.append({"file": cache_file.name, "reason": str(error)})
    records.extend(_target_rows(target))
    return records, rejected


def _candidate_reuse(
    candidates: list[dict[str, object]], available_dates: set[str], cache_dir: Path,
) -> tuple[list[dict[str, object]], list[dict[str, object]], list[dict[str, object]]]:
    matched_months = {
        str(item["requested_date"])[:7] for item in candidates
        if item["reason"] != "focus_alignment_candidate"
        and str(item["requested_date"]) in available_dates
    }
    reused = [
        item for item in candidates
        if (item["reason"] == "focus_alignment_candidate"
            and str(item["requested_date"]) in available_dates)
        or (item["reason"] != "focus_alignment_candidate"
            and str(item["requested_date"])[:7] in matched_months)
    ]
    planned = [item for item in candidates if item not in reused]
    cache_hits = [
        item for item in planned
        if (cache_dir / f"SPRD{date.fromisoformat(str(item['requested_date'])).strftime('%y%m%d')}.zip").exists()
    ]
    return planned, reused, cache_hits


def _build_plan(args: argparse.Namespace) -> tuple[dict[str, object], list[Path], list[dict[str, object]]]:
    final_month = args.end if args.include_current else _month_start(args.end, -1)
    start = args.start or _month_start(final_month, -(args.months - 1))
    if start > args.end:
        raise ValueError("--start must be on or before --end")
    candidates = monthly_candidates(
        start, args.end, include_current=args.include_current,
        fallback_days=args.fallback_days, focus_dates=args.focus_date,
    )
    paths = _input_paths(args.input)
    combined, cache_rejected = _source_inventory(paths, args.cache_dir, args.sqlite)
    observed_months = {str(row["trade_date"])[:7] for row in combined}
    local_months = {
        month for month in observed_months
        if start.strftime("%Y-%m") <= month <= args.end.strftime("%Y-%m")
    }
    available_dates = {str(row["trade_date"]) for row in combined}
    planned, reused, cache_hits = _candidate_reuse(candidates, available_dates, args.cache_dir)
    plan_dates = {str(item["requested_date"]) for item in candidates}
    unmatched = [
        {"month": month, "available_dates": sorted(d for d in available_dates if d[:7] == month),
         "reason": "outside planned candidate dates; not used to satisfy monthly plan"}
        for month in sorted(local_months)
        if not any(day[:7] == month for day in available_dates & plan_dates)
    ]
    report: dict[str, object] = {
        "dry_run": not args.execute,
        "source": "https://www.b3.com.br/pesquisapregao/download?filelist=SPRD{YYMMDD}.zip",
        "source_basis": "retrospective_reference",
        "start": start.isoformat(), "end": args.end.isoformat(),
        "months_requested": args.months, "include_current": args.include_current,
        "fallback_days": args.fallback_days,
        "candidate_dates": sorted(plan_dates), "local_covered_months": sorted(local_months),
        "reused_month_candidates": sorted({str(item["requested_date"]) for item in reused}),
        "local_dates_outside_candidate_window": unmatched,
        "cache_hit_candidates": sorted({str(item["requested_date"]) for item in cache_hits}),
        "rejected_cache_files": cache_rejected,
        "requests_planned_upper_bound": len({str(item["requested_date"]) for item in planned}
                                             - {str(item["requested_date"]) for item in cache_hits}),
        "minimum_interval_seconds": args.min_request_interval,
        "inventory": inventory(combined, start=start, end=args.end),
        "inserted_local": 0,
    }
    return report, paths, planned


def _execute_plan(
    args: argparse.Namespace, report: dict[str, object],
    paths: list[Path], planned: list[dict[str, object]],
) -> None:
    if not args.execute:
        return
    if paths:
        report["inserted_local"] = import_local_files(args.sqlite, paths, execute=True)["inserted"]
    report["collection"] = collect_candidates(
        planned, cache_dir=args.cache_dir, database=args.sqlite,
        min_interval_seconds=args.min_request_interval,
        fallback_days=args.fallback_days,
        progress=lambda event: print(
            json.dumps({"progress": event}, separators=(",", ":")),
            file=sys.stderr, flush=True,
        ),
    )


def main() -> int:
    args = _parse_args()
    try:
        report, paths, planned = _build_plan(args)
    except ValueError as error:
        raise SystemExit(str(error)) from error
    _execute_plan(args, report, paths, planned)
    print(json.dumps(report, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
