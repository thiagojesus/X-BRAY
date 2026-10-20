"""Local reference-history reconstruction; never represents point-in-time knowledge."""

from __future__ import annotations

import hashlib
import json
import sqlite3
import time
from collections import defaultdict
from collections.abc import Mapping
from contextlib import closing
from datetime import date, datetime, timedelta, timezone
from math import isfinite
from pathlib import Path
from typing import cast
from urllib.parse import urlparse

import httpx

from config import BCB_ODATA_BASE, BCB_SGS_BASE
from datafetchers.b3_di import (
    UnsupportedDIMaturityCalendar,
    require_verified_maturity_basis,
)
from datafetchers.focus import FOCUS_12M_FILTER
from derived.di_selection import select_di_contract_for_horizon
from derived.inflation_targets import INFLATION_TARGET_RULES
from derived.models import DerivedInput
from derived.real_rate_inputs import DIContract
from indicator_catalog import INDICATOR_CATALOG, IndicatorKey
from macro_score.methodology import COMPONENTS_BY_KEY
from macro_score.models import ComponentPoint, ComponentSeries
from macro_score.sources import (
    MacroScoreSources,
    RetrospectiveDIObservation,
    ScoreVintage,
    load_retrospective_di_observations,
    load_score_vintages,
)
from provider_http import JSON_MAX_BYTES

FORMULA_VERSION = "macro-retrospective-1.0.0"
DI_FORMULA_VERSION = "macro-retrospective-di-1.0.0"
# Quote units are documented, but maturity provenance/calendars remain
# unverified. DI eligibility is determined per observation and component.
MAX_FOCUS_PAGES = 100
TIMEOUT = 30.0
FOCUS_HOST = urlparse(BCB_ODATA_BASE).hostname
SOURCE_KEYS = frozenset(
    {
        "ibc_br_sa",
        "desemprego",
        "rendimento_medio_real",
        "ipca_12m",
        "saldo_primario_12m_pib",
        "dbgg_pib",
        "saldo_nominal_12m_pib",
        "inadimplencia_pf",
        "comprometimento_renda",
        "icc_pf",
        "taxa_media_credito_pf",
        "selic_meta",
        "idp_12m_pib",
        "conta_corrente_12m_pib",
        "reservas_internacionais",
    }
)


def _di_contracts_for_date(
    observations: tuple[RetrospectiveDIObservation, ...], trade_date: date,
) -> tuple[DIContract, ...] | None:
    rows = [item for item in observations if item.trade_date == trade_date]
    by_symbol: dict[str, list[RetrospectiveDIObservation]] = defaultdict(list)
    for item in rows:
        by_symbol[item.symbol].append(item)
    contracts: list[DIContract] = []
    for symbol, revisions in by_symbol.items():
        values = {(row.maturity, row.rate) for row in revisions}
        if len(values) != 1 or any(not isfinite(row.rate) for row in revisions):
            return None
        maturity, rate = next(iter(values))
        contracts.append(DIContract(
            trade_date=trade_date, symbol=symbol, maturity=maturity, rate=rate,
            available_at=max(row.fetched_at for row in revisions),
            availability_basis="retrospective_reference",
        ))
    return tuple(contracts)


def _focus_by_reference_month(vintages: tuple[ScoreVintage, ...]) -> dict[str, list[ScoreVintage]]:
    grouped: dict[str, list[ScoreVintage]] = defaultdict(list)
    for item in vintages:
        if item.provider != "focus" or item.series_key != "IPCA_12M_S_BASE0":
            continue
        month = item.reference_period.strftime("%Y-%m")
        grouped[month].append(item)
    return {month: sorted(items, key=lambda item: (item.reference_period, item.available_at))
            for month, items in grouped.items()}


def build_retrospective_di_values(
    sources: MacroScoreSources,
) -> tuple[list[tuple[object, ...]], dict[str, dict[str, int]]]:
    """Build monthly DI components from same-date, cutoff-eligible source pairs.

    Rates are consumed unchanged as the existing methodology does; no unit conversion
    or price fallback is performed. Revision conflicts invalidate that candidate date.
    """
    focus_months = _focus_by_reference_month(sources.vintages)
    di_dates: dict[str, list[date]] = defaultdict(list)
    for item in sources.retrospective_di:
        if item.trade_date not in di_dates[item.trade_date.strftime("%Y-%m")]:
            di_dates[item.trade_date.strftime("%Y-%m")].append(item.trade_date)
    rows: list[tuple[object, ...]] = []
    all_failures: dict[str, dict[str, int]] = {}
    for month in sorted(set(di_dates) | set(focus_months)):
        month_rows, reasons = _build_retrospective_di_month(
            month, sorted(di_dates.get(month, [])), focus_months.get(month, []), sources,
        )
        rows.extend(month_rows)
        if reasons:
            all_failures[month] = reasons
    return rows, all_failures


def eligible_retrospective_di_values(
    sources: MacroScoreSources, *, minimum_months: int = 24,
) -> list[tuple[object, ...]]:
    """Gate overlay by distinct valid months for each component, after selection."""
    candidates, _failures = build_retrospective_di_values(sources)
    months: dict[str, set[str]] = defaultdict(set)
    for row in candidates:
        if (len(row) == 10 and isinstance(row[1], str)
                and has_verified_di_maturities(str(row[7]))):
            months[str(row[0])].add(row[1])
    eligible_components = {
        component for component, references in months.items()
        if len(references) >= minimum_months
    }
    return [row for row in candidates if str(row[0]) in eligible_components]


def has_verified_di_maturities(inputs_json: str) -> bool:
    try:
        inputs: object = json.loads(inputs_json)
    except (TypeError, ValueError):
        return False
    if not isinstance(inputs, dict):
        return False
    inputs = cast(dict[str, object], inputs)
    trade_dates: set[str] = set()
    found = False
    for key in ("di_1y", "di_5y"):
        source = inputs.get(key)
        if source is None:
            continue
        found = True
        if not isinstance(source, dict):
            return False
        source = cast(dict[str, object], source)
        try:
            require_verified_maturity_basis(str(source.get("maturity_basis", "")))
        except UnsupportedDIMaturityCalendar:
            return False
        trade_date, maturity, rate = source.get("trade_date"), source.get("maturity"), source.get("rate")
        if not isinstance(trade_date, str) or not isinstance(maturity, str):
            return False
        try:
            date.fromisoformat(trade_date)
            date.fromisoformat(maturity)
        except ValueError:
            return False
        if isinstance(rate, bool) or not isinstance(rate, (int, float)) or not isfinite(float(rate)):
            return False
        trade_dates.add(trade_date)
    return found and len(trade_dates) == 1


def _gate_additional_overlay_values(
    values: list[tuple[object, ...]], minimum_months: int = 24,
) -> list[tuple[object, ...]]:
    di_rows = [row for row in values if len(row) == 10 and str(row[4]) == DI_FORMULA_VERSION]
    counts: dict[str, set[str]] = defaultdict(set)
    for row in di_rows:
        if isinstance(row[1], str) and has_verified_di_maturities(str(row[7])):
            counts[str(row[0])].add(row[1])
    eligible = {key for key, months in counts.items() if len(months) >= minimum_months}
    return [
        row for row in values
        if len(row) != 10 or str(row[4]) != DI_FORMULA_VERSION
        or str(row[0]) in eligible
    ]


def _build_retrospective_di_month(
    month: str, month_dates: list[date], focus_candidates: list[ScoreVintage],
    sources: MacroScoreSources,
) -> tuple[list[tuple[object, ...]], dict[str, int]]:
    reasons: dict[str, int] = defaultdict(int)
    if not month_dates:
        return [], {"di_curve_missing": 1}
    if not focus_candidates:
        return [], {"focus_12m_base0_missing": 1}
    reference_date = min(month_dates[-1], focus_candidates[-1].reference_period)
    curve_date = max((day for day in month_dates if day <= reference_date), default=None)
    if curve_date is None:
        return [], {"di_curve_after_aligned_reference": 1}
    reference_date = min(reference_date, curve_date)
    eligible_focus = [item for item in focus_candidates if item.reference_period <= reference_date]
    if not eligible_focus:
        return [], {"focus_reference_after_di_trade_date": 1}
    focus = eligible_focus[-1]
    contracts = _di_contracts_for_date(sources.retrospective_di, curve_date)
    if contracts is None:
        return [], {"conflicting_di_revision": 1}
    if not contracts:
        return [], {"di_curve_missing": 1}
    if any(
        item.maturity_basis != "b3_official_calendar_v1"
        for item in sources.retrospective_di if item.trade_date == curve_date
    ):
        reasons["maturity_calendar_unverified"] += 1
    nominal = select_di_contract_for_horizon(contracts, reference_date, 365)
    one_year = select_di_contract_for_horizon(contracts, reference_date, 365)
    five_year = select_di_contract_for_horizon(contracts, reference_date, 1825)
    values = _di_month_values(
        focus, nominal, one_year, five_year, sources.retrospective_di, reasons,
    )
    return _retrospective_di_month_rows(month, reference_date, values, focus), dict(reasons)


def _di_month_values(
    focus: ScoreVintage, nominal: DIContract | None,
    one_year: DIContract | None, five_year: DIContract | None,
    observations: tuple[RetrospectiveDIObservation, ...], reasons: dict[str, int],
) -> dict[str, tuple[float, str, dict[str, object]]]:
    values: dict[str, tuple[float, str, dict[str, object]]] = {}
    denominator = 1 + focus.value / 100
    if nominal is None:
        reasons["di_1y_outside_tolerance"] += 1
    elif denominator == 0:
        reasons["invalid_fisher_denominator"] += 1
    else:
        values["juro_real_ex_ante_1y"] = (
            ((1 + nominal.rate / 100) / denominator - 1) * 100,
            "pct_annual", {"di_1y": _di_source_provenance(nominal, observations),
                           "focus_12m_base0": _focus_provenance(focus)},
        )
    if one_year is None or five_year is None:
        reasons["di_slope_contract_outside_tolerance"] += 1
    elif (one_year.symbol, one_year.maturity) == (five_year.symbol, five_year.maturity):
        reasons["di_slope_contract_reused"] += 1
    else:
        values["inclinacao_di_5y_1y"] = (
            five_year.rate - one_year.rate, "pp",
            {"di_1y": _di_source_provenance(one_year, observations),
             "di_5y": _di_source_provenance(five_year, observations)},
        )
    return values


def _retrospective_di_month_rows(
    month: str, reference_date: date,
    values: dict[str, tuple[float, str, dict[str, object]]], focus: ScoreVintage,
) -> list[tuple[object, ...]]:
    output: list[tuple[object, ...]] = []
    for component, (value, unit, source_inputs) in values.items():
        inputs = {"reference_date": reference_date.isoformat(), **source_inputs}
        stamps: list[datetime] = [focus.available_at]
        for source in source_inputs.values():
            if isinstance(source, Mapping):
                source_values = cast(Mapping[str, object], source)
                stamp = source_values.get("fetched_at") or source_values.get("available_at")
                if isinstance(stamp, str):
                    try:
                        stamps.append(datetime.fromisoformat(stamp.replace("Z", "+00:00")))
                    except ValueError:
                        continue
        fetched_at = max(stamps).astimezone(timezone.utc).isoformat()
        output.append((
            component, month, value, unit, DI_FORMULA_VERSION,
            "retrospective_reference", fetched_at,
            json.dumps(inputs, sort_keys=True, separators=(",", ":")), _digest(inputs), None,
        ))
    return output


def reconstruct_di_ledger(
    path: Path, *, as_of: datetime | None = None, execute: bool = False,
) -> dict[str, object]:
    """Calculate two DI macro series from a local DI ledger and existing Focus vintages."""
    from macro_score.retrospective_di import validate_retrospective_target

    target = Path(path)
    validate_retrospective_target(target)
    cutoff = as_of or datetime.now(timezone.utc)
    if cutoff.tzinfo is None or cutoff.utcoffset() is None:
        raise ValueError("as_of must be timezone-aware")
    cutoff = cutoff.astimezone(timezone.utc)
    if not target.exists():
        raise ValueError("retrospective DI SQLite ledger does not exist")
    with closing(sqlite3.connect(f"{target.as_uri()}?mode=ro", uri=True)) as connection:
        connection.row_factory = sqlite3.Row
        columns = {
            str(row[1]) for row in connection.execute(
                "PRAGMA table_info(retrospective_di_observations)"
            ).fetchall()
        }
        has_maturity_basis = "maturity_basis" in columns
        maturity_column = ",maturity_basis" if has_maturity_basis else ""
        rows = connection.execute(
            "SELECT trade_date,symbol,maturity,rate,source_hash,source_name,source_member,fetched_at,source_basis" + maturity_column + " "
            "FROM retrospective_di_observations WHERE source_basis=? AND fetched_at<=?",
            ("retrospective_reference", cutoff.isoformat()),
        ).fetchall()
    observations: list[RetrospectiveDIObservation] = []
    for row in rows:
        try:
            observations.append(RetrospectiveDIObservation(
                date.fromisoformat(row["trade_date"]), row["symbol"],
                date.fromisoformat(row["maturity"]), float(row["rate"]),
                row["source_hash"], row["source_name"], row["source_member"],
                datetime.fromisoformat(row["fetched_at"]).astimezone(timezone.utc),
                row["source_basis"],
                row["maturity_basis"] if has_maturity_basis else "legacy_local_unverified",
            ))
        except (TypeError, ValueError):
            continue
    sources = MacroScoreSources(
        vintages=load_score_vintages(as_of=cutoff), target_observations=(),
        derived_snapshots=(), retrospective_di=tuple(observations),
    )
    calculated, failures = build_retrospective_di_values(sources)
    eligible = eligible_retrospective_di_values(sources)
    by_component: dict[str, set[str]] = defaultdict(set)
    for row in calculated:
        by_component[str(row[0])].add(str(row[1]))
    counts = {key: len(months) for key, months in sorted(by_component.items())}
    eligible_by_component: dict[str, set[str]] = defaultdict(set)
    for row in eligible:
        eligible_by_component[str(row[0])].add(str(row[1]))
    eligible_counts = {
        key: len(eligible_by_component.get(key, set())) for key in sorted(counts)
    }
    inserted = 0
    if execute:
        with closing(sqlite3.connect(target)) as connection:
            inserted = _insert_rows(connection, calculated)
    return {
        "dry_run": not execute, "source_basis": "retrospective_reference",
        "point_in_time": False, "formula_version": DI_FORMULA_VERSION,
        "cutoff": cutoff.isoformat(), "counts": counts,
        "candidate_distinct_months": counts,
        "eligible_distinct_months": eligible_counts,
        "months_threshold_met": {key: count >= 24 for key, count in eligible_counts.items()},
        "percentile_eligible": {key: count >= 24 for key, count in eligible_counts.items()},
        "eligibility_blocker": (
            None if all(count >= 24 for count in eligible_counts.values())
            else "maturity provenance or 24 eligible months missing for one or more components"
        ),
        "failures_by_month": failures, "inserted": inserted,
    }


def _di_source_provenance(
    selected: DIContract, observations: tuple[RetrospectiveDIObservation, ...],
) -> dict[str, object]:
    rows = [item for item in observations if item.trade_date == selected.trade_date and item.symbol == selected.symbol]
    return {
        "symbol": selected.symbol, "maturity": selected.maturity.isoformat(),
        "maturity_basis": (
            rows[0].maturity_basis
            if len({item.maturity_basis for item in rows}) == 1
            else "mixed_or_conflicting_provenance"
        ),
        "trade_date": selected.trade_date.isoformat(),
        "rate": selected.rate, "source_hashes": sorted({item.source_hash for item in rows}),
        "source_files": sorted({item.source_name for item in rows}),
        "fetched_at": max(item.fetched_at for item in rows).isoformat(),
    }


def _focus_provenance(vintage: ScoreVintage) -> dict[str, object]:
    return {
        "reference_date": vintage.reference_period.isoformat(), "value": vintage.value,
        "available_at": vintage.available_at.isoformat(),
        "payload_hash": vintage.payload_hash, "source": vintage.source,
    }


def _month(value: str) -> str:
    parsed = date.fromisoformat(value[:10])
    return parsed.strftime("%Y-%m")


def _date_arg(value: date) -> str:
    return value.strftime("%d/%m/%Y")


def _digest(value: object) -> str:
    raw = json.dumps(value, sort_keys=True, separators=(",", ":"), allow_nan=False)
    return hashlib.sha256(raw.encode()).hexdigest()


def _get(
    client: httpx.Client, url: str, *, params: dict[str, str] | None = None
) -> httpx.Response:
    for attempt in range(3):
        try:
            response = client.get(url, params=params)
            if response.status_code != 404:
                response.raise_for_status()
            if len(response.content) > JSON_MAX_BYTES:
                raise ValueError(
                    "official source response exceeded the configured size limit"
                )
            return response
        except (httpx.TimeoutException, httpx.ConnectError):
            if attempt == 2:
                raise
            time.sleep(0.2 * (attempt + 1))
    raise RuntimeError("unreachable retry state")


def _focus_pages(client: httpx.Client) -> list[dict[str, object]]:
    base = f"{BCB_ODATA_BASE}/ExpectativasMercadoInflacao12Meses"
    params: dict[str, str] | None = {
        "$filter": FOCUS_12M_FILTER,
        "$orderby": "Data desc",
        "$top": "1000",
        "$format": "json",
    }
    records: list[dict[str, object]] = []
    for _ in range(MAX_FOCUS_PAGES):
        response = _get(client, base, params=params)
        body = response.json()
        if not isinstance(body, dict):
            raise TypeError("Focus response has an invalid shape")
        response_body = cast(dict[str, object], body)
        if not isinstance(response_body.get("value"), list):
            raise TypeError("Focus response has an invalid shape")
        response_rows = cast(list[object], response_body["value"])
        if any(not isinstance(row, dict) for row in response_rows):
            raise TypeError("Focus response contains a non-object observation")
        records.extend(cast(dict[str, object], row) for row in response_rows)
        next_link = response_body.get("@odata.nextLink")
        if not next_link:
            return records
        parsed = urlparse(str(next_link))
        if parsed.scheme != "https" or parsed.hostname != FOCUS_HOST:
            raise ValueError("Focus continuation link is outside the official BCB host")
        params = None
        base = str(next_link)
    raise ValueError("Focus pagination exceeded the safety page limit")


def _load_sgs(
    client: httpx.Client, code: int, start: date, end: date
) -> list[dict[str, object]]:
    base = BCB_SGS_BASE.format(code=code)
    response = _get(
        client,
        base,
        params={
            "formato": "json",
            "dataInicial": _date_arg(start),
            "dataFinal": _date_arg(end),
        },
    )
    if response.status_code == 404:
        body = response.json()
        body_dict = cast(dict[str, object], body) if isinstance(body, dict) else {}
        error = body_dict.get("erro")
        error_dict = cast(dict[str, object], error) if isinstance(error, dict) else {}
        detail = error_dict.get("detail")
        if isinstance(detail, str) and detail.endswith("Value(s) not found"):
            return []
        response.raise_for_status()
    payload = response.json()
    if not isinstance(payload, list):
        raise TypeError(f"SGS {code} response is not a list")
    payload_rows = cast(list[object], payload)
    if any(not isinstance(row, dict) for row in payload_rows):
        raise TypeError(f"SGS {code} response contains a non-object observation")
    return [cast(dict[str, object], row) for row in payload_rows]


def _insert_rows(connection: sqlite3.Connection, rows: list[tuple[object, ...]]) -> int:
    connection.execute("""CREATE TABLE IF NOT EXISTS retrospective_macro_values (
        component_key TEXT NOT NULL, reference_month TEXT NOT NULL, value REAL, unit TEXT NOT NULL,
        formula_version TEXT NOT NULL, source_basis TEXT NOT NULL, fetched_at TEXT NOT NULL,
        inputs_json TEXT NOT NULL, inputs_hash TEXT NOT NULL, reason TEXT,
        PRIMARY KEY (component_key, reference_month, formula_version, inputs_hash))""")
    before = connection.total_changes
    connection.executemany(
        """INSERT OR IGNORE INTO retrospective_macro_values
        (component_key,reference_month,value,unit,formula_version,source_basis,fetched_at,inputs_json,inputs_hash,reason)
        VALUES (?,?,?,?,?,?,?,?,?,?)""",
        rows,
    )
    connection.commit()
    return connection.total_changes - before


def _validate_reconstruction_target(path: Path, start: date, end: date) -> None:
    ten_year_limit = _shift_year(start, 10) + timedelta(days=31)
    if start > end or end > ten_year_limit or end > datetime.now(timezone.utc).date():
        raise ValueError(
            "date range must be ordered, current, and no longer than ten years"
        )
    if not path.is_absolute() or path.suffix != ".db":
        raise ValueError("an absolute local .db target path is required")
    default_path = Path(__file__).resolve().parents[1] / "xbry.db"
    if path.resolve() == default_path.resolve():
        raise ValueError("refusing to write to the application's default database")


def _fetch_observations(
    start: date, end: date
) -> tuple[
    dict[str, dict[str, tuple[float, dict[str, object]]]],
    dict[str, tuple[float, dict[str, object]]],
]:
    observations: dict[str, dict[str, tuple[float, dict[str, object]]]] = defaultdict(
        dict
    )
    input_start = _shift_month(start, -12)
    cutoff = datetime.now(timezone.utc).date()
    with httpx.Client(timeout=TIMEOUT, follow_redirects=False) as client:
        for key in sorted(SOURCE_KEYS | {"idp_12m_pib"}):
            spec = INDICATOR_CATALOG[IndicatorKey(key)]
            for window_start, window_end in _date_windows(input_start, end):
                for row in _load_sgs(
                    client, int(spec.provider_code), window_start, window_end
                ):
                    day, raw_value = row.get("data"), row.get("valor")
                    if not isinstance(day, str):
                        raise TypeError(
                            f"invalid SGS date for {key} ({spec.provider_code}) "
                            f"in window {window_start}..{window_end}: {row!r}"
                        )
                    try:
                        day_text, month_text, year_text = day.split("/")
                        reference = date(int(year_text), int(month_text), int(day_text))
                    except ValueError as exc:
                        raise ValueError(
                            f"unparseable SGS date for {key} ({spec.provider_code}) "
                            f"in window {window_start}..{window_end}: {day!r}"
                        ) from exc
                    if not window_start <= reference <= window_end:
                        raise ValueError(
                            f"SGS reference {reference} is outside requested window "
                            f"{window_start}..{window_end} for {key} ({spec.provider_code}); "
                            "response rejected before persistence"
                        )
                    if not input_start <= reference <= end or reference > cutoff:
                        raise ValueError(
                            f"SGS reference {reference} is outside allowed reconstruction "
                            f"range {input_start}..{min(end, cutoff)} for {key} "
                            f"({spec.provider_code}); response rejected before persistence"
                        )
                    if isinstance(raw_value, bool) or not isinstance(
                        raw_value, (int, float, str)
                    ):
                        raise TypeError(
                            f"invalid SGS numeric value for {key} ({spec.provider_code}) "
                            f"at {reference}: {raw_value!r}"
                        )
                    try:
                        value = float(raw_value)
                    except (ValueError, TypeError) as exc:
                        raise ValueError(
                            f"invalid SGS numeric value for {key} ({spec.provider_code}) "
                            f"at {reference}: {raw_value!r}"
                        ) from exc
                    if not isfinite(value):
                        raise ValueError(
                            f"non-finite SGS numeric value for {key} ({spec.provider_code}) "
                            f"at {reference}: {raw_value!r}"
                        )
                    observations[key][reference.strftime("%Y-%m")] = (value, row)
        focus = _focus_pages(client)
    return observations, _select_focus_months(focus, start, end, cutoff=cutoff)


def _shift_month(value: date, months: int) -> date:
    index = value.year * 12 + value.month - 1 + months
    year, month = divmod(index, 12)
    day = min(value.day, _days_in_month(year, month + 1))
    return date(year, month + 1, day)


def _days_in_month(year: int, month: int) -> int:
    if month == 12:
        return (date(year + 1, 1, 1) - timedelta(days=1)).day
    return (date(year, month + 1, 1) - timedelta(days=1)).day


def _shift_year(value: date, years: int) -> date:
    try:
        return value.replace(year=value.year + years)
    except ValueError:
        return value.replace(year=value.year + years, day=28)


def _date_windows(start: date, end: date) -> tuple[tuple[date, date], ...]:
    windows: list[tuple[date, date]] = []
    cursor = start
    while cursor <= end:
        window_end = min(_shift_year(cursor, 10), end)
        windows.append((cursor, window_end))
        if window_end == end:
            break
        # SGS monthly series can report the boundary observation even when a
        # later window starts the next day. Overlap that boundary day; strict
        # validation remains per-window and duplicates collapse by reference.
        cursor = window_end
    return tuple(windows)


def _select_focus_months(
    records: list[dict[str, object]],
    start: date,
    end: date,
    *,
    cutoff: date | None = None,
) -> dict[str, tuple[float, dict[str, object]]]:
    allowed_cutoff = cutoff or datetime.now(timezone.utc).date()
    selected: dict[str, tuple[float, dict[str, object]]] = {}
    for row in records:
        if (
            row.get("Indicador") != "IPCA"
            or row.get("Suavizada") != "S"
            or row.get("baseCalculo") != 0
        ):
            continue
        day, median = row.get("Data"), row.get("Mediana")
        if (
            not isinstance(day, str)
            or isinstance(median, bool)
            or not isinstance(median, (int, float))
            or not isfinite(float(median))
        ):
            continue
        try:
            reference = date.fromisoformat(day[:10])
        except ValueError:
            raise ValueError(f"unparseable Focus reference date: {day!r}")
        if reference > allowed_cutoff:
            raise ValueError(
                f"Focus reference {reference} exceeds current cutoff {allowed_cutoff}; "
                "response rejected before persistence"
            )
        month = _month(day)
        if reference > end or not start.strftime("%Y-%m") <= month <= end.strftime(
            "%Y-%m"
        ):
            continue
        previous = selected.get(month)
        if previous is None or day > str(previous[1].get("Data", "")):
            selected[month] = (float(median), row)
    return selected


def _month_inputs(
    observations: dict[str, dict[str, tuple[float, dict[str, object]]]],
) -> dict[str, dict[str, tuple[float, object]]]:
    monthly: dict[str, dict[str, tuple[float, object]]] = defaultdict(dict)
    for key, periods in observations.items():
        spec = INDICATOR_CATALOG[IndicatorKey(key)]
        for month, (value, source_row) in periods.items():
            monthly[month][key] = (
                -value
                if spec.transformation and spec.transformation.value == "invert_sign"
                else value,
                {
                    "source_url": spec.source_url,
                    "provider_code": int(spec.provider_code),
                    "record": source_row,
                },
            )
    return monthly


def _raw(inputs: dict[str, tuple[float, object]], key: str) -> float | None:
    value = inputs.get(key)
    return value[0] if value else None


def _level_values(
    inputs: dict[str, tuple[float, object]],
) -> dict[str, tuple[float, str, dict[str, object]]]:
    result: dict[str, tuple[float, str, dict[str, object]]] = {}
    direct = (
        "desemprego",
        "dbgg_pib",
        "saldo_nominal_12m_pib",
        "saldo_primario_12m_pib",
        "inadimplencia_pf",
        "comprometimento_renda",
        "icc_pf",
        "conta_corrente_12m_pib",
        "reservas_internacionais",
    )
    for key in direct:
        value = _raw(inputs, key)
        if value is not None:
            result[key] = (
                value,
                INDICATOR_CATALOG[IndicatorKey(key)].unit.value,
                {key: inputs[key][1]},
            )
    return result


def _change_values(
    month: str,
    inputs: dict[str, tuple[float, object]],
    monthly: dict[str, dict[str, tuple[float, object]]],
) -> dict[str, tuple[float, str, dict[str, object]]]:
    previous = monthly.get(_previous_year(month), {})
    pairs = (
        ("ibc_br_sa", "ibc_br_sa_yoy", "pct", "ratio"),
        ("rendimento_medio_real", "rendimento_real_yoy", "pct", "ratio"),
        ("dbgg_pib", "dbgg_yoy_pp", "pp", "difference"),
        ("saldo_primario_12m_pib", "variacao_saldo_primario_12m", "pp", "difference"),
    )
    result: dict[str, tuple[float, str, dict[str, object]]] = {}
    for key, component, unit, operation in pairs:
        current, prior = _raw(inputs, key), _raw(previous, key)
        if current is None or prior is None or (operation == "ratio" and prior == 0):
            continue
        value = (current / prior - 1) * 100 if operation == "ratio" else current - prior
        result[component] = (
            value,
            unit,
            {"current": inputs[key][1], "previous": previous[key][1]},
        )
    return result


def _paired_values(
    inputs: dict[str, tuple[float, object]],
) -> dict[str, tuple[float, str, dict[str, object]]]:
    result: dict[str, tuple[float, str, dict[str, object]]] = {}
    credit, selic = _raw(inputs, "taxa_media_credito_pf"), _raw(inputs, "selic_meta")
    if credit is not None and selic is not None:
        result["spread_credito_pf_selic"] = (
            credit - selic,
            "pp",
            {
                "credit": inputs["taxa_media_credito_pf"][1],
                "selic": inputs["selic_meta"][1],
            },
        )
    cc, idp = _raw(inputs, "conta_corrente_12m_pib"), _raw(inputs, "idp_12m_pib")
    if cc is not None and idp is not None and cc < 0:
        result["cobertura_externa"] = (
            idp / abs(cc),
            "ratio",
            {
                "current_account": inputs["conta_corrente_12m_pib"][1],
                "idp": inputs["idp_12m_pib"][1],
            },
        )
    return result


def _target_values(
    month: str,
    inputs: dict[str, tuple[float, object]],
    focus: dict[str, tuple[float, dict[str, object]]],
) -> dict[str, tuple[float, str, dict[str, object]]]:
    reference = date.fromisoformat(f"{month}-01")
    rule = next(
        (
            item
            for item in INFLATION_TARGET_RULES
            if item.effective_from <= reference
            and (item.effective_to is None or reference < item.effective_to)
        ),
        None,
    )
    if rule is None:
        return {}
    result: dict[str, tuple[float, str, dict[str, object]]] = {}
    ipca = _raw(inputs, "ipca_12m")
    if ipca is not None:
        result["abs_ipca_12m_target"] = (
            abs(ipca - rule.center_pct),
            "pp",
            {"ipca_12m": inputs["ipca_12m"][1], "target_rule": rule.rule_id},
        )
    expected = focus.get(month)
    if expected is not None:
        result["abs_focus_12m_target"] = (
            abs(expected[0] - rule.center_pct),
            "pp",
            {"focus": expected[1], "target_rule": rule.rule_id},
        )
        result["distancia_focus_meta"] = (
            expected[0] - rule.center_pct,
            "pp",
            {"focus": expected[1], "target_rule": rule.rule_id},
        )
    return result


def _build_rows(
    monthly: dict[str, dict[str, tuple[float, object]]],
    focus: dict[str, tuple[float, dict[str, object]]],
    fetched_at: str,
    *,
    output_start: date | None = None,
    output_end: date | None = None,
) -> list[tuple[object, ...]]:
    rows: list[tuple[object, ...]] = []
    for month, inputs in sorted(monthly.items()):
        reference_month = date.fromisoformat(f"{month}-01")
        if output_start is not None and reference_month < output_start.replace(day=1):
            continue
        if output_end is not None and reference_month > output_end.replace(day=1):
            continue
        values = {
            **_level_values(inputs),
            **_change_values(month, inputs, monthly),
            **_paired_values(inputs),
            **_target_values(month, inputs, focus),
        }
        for component, (value, unit, source_inputs) in values.items():
            packed = json.dumps(
                source_inputs, sort_keys=True, separators=(",", ":"), default=str
            )
            rows.append(
                (
                    component,
                    month,
                    value,
                    unit,
                    FORMULA_VERSION,
                    "retrospective_reference",
                    fetched_at,
                    packed,
                    _digest(source_inputs),
                    None,
                )
            )
    return rows


def _report(
    path: Path,
    rows: list[tuple[object, ...]],
    focus_months: int,
    dry_run: bool,
    fetched_at: str,
) -> dict[str, object]:
    if dry_run:
        counts: dict[str, int] = defaultdict(int)
        for row in rows:
            counts[str(row[0])] += 1
        return {
            "dry_run": True,
            "fetched_at": fetched_at,
            "source_basis": "retrospective_reference",
            "counts": dict(sorted(counts.items())),
            "focus_months": focus_months,
            "inserted": 0,
        }
    path.parent.mkdir(parents=True, exist_ok=True)
    with closing(sqlite3.connect(path)) as connection:
        inserted = _insert_rows(connection, rows)
        counts = dict(
            connection.execute(
                "SELECT component_key,COUNT(DISTINCT reference_month) FROM retrospective_macro_values WHERE formula_version=? GROUP BY component_key",
                (FORMULA_VERSION,),
            ).fetchall()
        )
    return {
        "dry_run": False,
        "fetched_at": fetched_at,
        "source_basis": "retrospective_reference",
        "counts": counts,
        "inserted": inserted,
        "coverage_24_months": {key: count >= 24 for key, count in counts.items()},
    }


def reconstruct(
    path: Path, *, start: date, end: date, dry_run: bool = True
) -> dict[str, object]:
    """Fetch official non-DI series into an explicitly selected local SQLite DB."""
    _validate_reconstruction_target(path, start, end)
    observations, focus = _fetch_observations(start, end)
    fetched_at = datetime.now(timezone.utc).isoformat()
    monthly = _month_inputs(observations)
    rows = _build_rows(monthly, focus, fetched_at, output_start=start, output_end=end)
    return _report(path, rows, len(focus), dry_run, fetched_at)


def _previous_year(month: str) -> str:
    year, number = (int(part) for part in month.split("-"))
    return f"{year - 1:04d}-{number:02d}"


def query_retrospective_history(start: str, end: str) -> tuple[dict[str, object], ...]:
    """Read only the explicitly marked reconstructed reference ledger."""
    from db import store

    rows = store.query_rows(
        "SELECT component_key, reference_month, value, unit, formula_version, source_basis, "
        "fetched_at, inputs_json, inputs_hash, reason FROM retrospective_macro_values "
        "WHERE reference_month >= ? AND reference_month <= ? AND source_basis = ? "
        "ORDER BY component_key, reference_month, fetched_at DESC, inputs_hash DESC",
        (start, end, "retrospective_reference"),
    )
    candidates: dict[tuple[str, str], list[dict[str, object]]] = defaultdict(list)
    for row in rows:
        candidates[(str(row["component_key"]), str(row["reference_month"]))].append(row)
    selected: list[dict[str, object]] = []
    for identity in sorted(candidates):
        versions = candidates[identity]
        newest_at = max(str(row["fetched_at"]) for row in versions)
        newest = [row for row in versions if str(row["fetched_at"]) == newest_at]
        fingerprints = {
            (str(row["formula_version"]), str(row["inputs_hash"]),
             str(row["value"]), str(row["unit"]))
            for row in newest
        }
        if len(fingerprints) == 1:
            selected.append(newest[0])
    grouped_history: dict[str, dict[str, dict[str, object]]] = defaultdict(dict)
    for record in selected:
        _select_overlay_row(grouped_history, record)
    cutoff = datetime.now(timezone.utc)
    sources = MacroScoreSources(
        vintages=load_score_vintages(as_of=cutoff), target_observations=(),
        derived_snapshots=(),
        retrospective_di=load_retrospective_di_observations(as_of=cutoff),
    )
    calculated, _failures = build_retrospective_di_values(sources)
    for value_row in calculated:
        month = str(value_row[1])
        if not start <= month <= end:
            continue
        _select_overlay_row(grouped_history, {
            "component_key": value_row[0], "reference_month": month,
            "value": value_row[2], "unit": value_row[3],
            "formula_version": value_row[4], "source_basis": value_row[5],
            "fetched_at": value_row[6], "inputs_json": value_row[7],
            "inputs_hash": value_row[8], "reason": value_row[9],
        })
    return tuple(
        grouped_history[component][month]
        for component in sorted(grouped_history)
        for month in sorted(grouped_history[component])
        if grouped_history[component][month].get("_ambiguous") is not True
    )


def overlay_retrospective_history(
    series: tuple[ComponentSeries, ...], *, as_of: datetime,
    additional_values: list[tuple[object, ...]] | None = None,
) -> tuple[ComponentSeries, ...]:
    """Use reconstructed monthly references for present-day normalization only."""
    from dataclasses import replace

    from db import store

    rows = store.query_rows(
        "SELECT component_key, reference_month, value, unit, fetched_at, inputs_hash, formula_version "
        "FROM retrospective_macro_values WHERE source_basis = ? AND formula_version = ? "
        "AND value IS NOT NULL AND fetched_at <= ? ORDER BY component_key, reference_month, fetched_at DESC, inputs_hash DESC",
        ("retrospective_reference", FORMULA_VERSION, as_of.isoformat()),
    )
    by_component: dict[str, dict[str, dict[str, object]]] = defaultdict(dict)
    for row in rows:
        _select_overlay_row(by_component, row)
    for values in _gate_additional_overlay_values(additional_values or []):
        if len(values) != 10 or not isinstance(values[6], str):
            continue
        try:
            available_at = datetime.fromisoformat(values[6].replace("Z", "+00:00"))
        except ValueError:
            continue
        if available_at > as_of:
            continue
        row = {
            "component_key": values[0], "reference_month": values[1],
            "value": values[2], "unit": values[3], "formula_version": values[4],
            "source_basis": values[5], "fetched_at": values[6],
            "inputs_json": values[7], "inputs_hash": values[8], "reason": values[9],
        }
        _select_overlay_row(by_component, row)
    result: list[ComponentSeries] = []
    for item in series:
        values = by_component.get(str(item.key))
        if not values:
            result.append(item)
            continue
        # One normalized sample has exactly one source basis. When retrospective
        # observations are available for this component, never blend them with
        # forward-captured production snapshots; the live current point remains intact.
        points_by_month: dict[str, ComponentPoint] = {}
        for month, row in sorted(values.items()):
            if row.get("_ambiguous") is True:
                continue
            ref = date.fromisoformat(f"{month}-01")
            fetched = datetime.fromisoformat(str(row["fetched_at"]))
            raw_value = row["value"]
            if not isinstance(raw_value, (int, float)):
                continue
            input_value = float(raw_value)
            provenance = DerivedInput(
                key="retrospective_reference",
                value=input_value,
                unit=str(row["unit"]),
                reference_period=ref.isoformat(),
                available_at=fetched,
                source="BCB official historical reference; not point-in-time",
                metadata=(
                    ("source_basis", "retrospective_reference"),
                    ("formula_version", str(row["formula_version"])),
                    ("inputs_hash", str(row["inputs_hash"])),
                ),
            )
            points_by_month[month] = ComponentPoint(
                value=input_value,
                unit=COMPONENTS_BY_KEY[item.key].unit,
                reference_period=ref,
                available_at=fetched,
                input_fingerprint=str(row["inputs_hash"]),
                inputs=(provenance,),
            )
        result.append(
            replace(
                item,
                history=tuple(points_by_month[key] for key in sorted(points_by_month)),
            )
        )
    return tuple(result)


def _select_overlay_row(
    grouped: dict[str, dict[str, dict[str, object]]], row: dict[str, object],
) -> None:
    component = str(row["component_key"])
    month = str(row["reference_month"])
    previous = grouped[component].get(month)
    if previous is not None and previous.get("_ambiguous") is True:
        return
    if previous is None:
        grouped[component][month] = row
        return
    try:
        incoming_at = datetime.fromisoformat(str(row["fetched_at"]).replace("Z", "+00:00"))
        previous_at = datetime.fromisoformat(str(previous["fetched_at"]).replace("Z", "+00:00"))
    except ValueError:
        return
    if incoming_at > previous_at:
        grouped[component][month] = row
    elif incoming_at == previous_at and (
        row.get("inputs_hash") != previous.get("inputs_hash")
        or row.get("formula_version") != previous.get("formula_version")
        or row.get("value") != previous.get("value")
        or row.get("unit") != previous.get("unit")
    ):
        grouped[component][month] = {"_ambiguous": True}
