import hashlib
import json
import os
import sqlite3
import threading
from datetime import date, datetime, timezone
from math import isfinite
from pathlib import Path

from config import BASE_DIR
from db.migrations import POSTGRES_SCHEMA_LOCK_KEY, run_migrations
from domain_contracts import Record

DB_PATH = Path(os.environ.get("XBRAY_SQLITE_PATH", str(BASE_DIR / "xbry.db"))).expanduser()
DATABASE_URL = os.environ.get("DATABASE_URL", "")

_conn: sqlite3.Connection | None = None
_pg_local = threading.local()
_sqlite_lock = threading.RLock()

SQLITE_SCHEMA = """
    CREATE TABLE IF NOT EXISTS sgs (
        series_code INTEGER NOT NULL,
        date TEXT NOT NULL,
        value REAL,
        fetched_at TEXT NOT NULL DEFAULT (datetime('now')),
        PRIMARY KEY (series_code, date)
    );

    CREATE TABLE IF NOT EXISTS anbima (
        sheet_name TEXT NOT NULL,
        row_index INTEGER NOT NULL,
        date TEXT,
        data_json TEXT NOT NULL,
        fetched_at TEXT NOT NULL DEFAULT (datetime('now')),
        PRIMARY KEY (sheet_name, row_index)
    );

    CREATE TABLE IF NOT EXISTS focus (
        indicator TEXT NOT NULL,
        date TEXT NOT NULL,
        data_json TEXT NOT NULL,
        fetched_at TEXT NOT NULL DEFAULT (datetime('now')),
        PRIMARY KEY (indicator, date)
    );

    CREATE TABLE IF NOT EXISTS b3_di (
        trade_date TEXT NOT NULL,
        symbol TEXT NOT NULL,
        maturity TEXT NOT NULL,
        rate REAL,
        fetched_at TEXT NOT NULL DEFAULT (datetime('now')),
        PRIMARY KEY (trade_date, symbol)
    );

    CREATE TABLE IF NOT EXISTS treasury (
        bond_code INTEGER NOT NULL,
        base_date TEXT NOT NULL,
        data_json TEXT NOT NULL,
        fetched_at TEXT NOT NULL DEFAULT (datetime('now')),
        PRIMARY KEY (bond_code, base_date)
    );
    CREATE INDEX IF NOT EXISTS treasury_base_date_idx ON treasury (base_date);

    CREATE TABLE IF NOT EXISTS meta (
        key TEXT PRIMARY KEY,
        value TEXT
    );
"""

PG_SCHEMA = """
    CREATE TABLE IF NOT EXISTS sgs (
        series_code INTEGER NOT NULL,
        date TEXT NOT NULL,
        value DOUBLE PRECISION,
        fetched_at TEXT NOT NULL DEFAULT (now()),
        PRIMARY KEY (series_code, date)
    );

    CREATE TABLE IF NOT EXISTS anbima (
        sheet_name TEXT NOT NULL,
        row_index INTEGER NOT NULL,
        date TEXT,
        data_json TEXT NOT NULL,
        fetched_at TEXT NOT NULL DEFAULT (now()),
        PRIMARY KEY (sheet_name, row_index)
    );

    CREATE TABLE IF NOT EXISTS focus (
        indicator TEXT NOT NULL,
        date TEXT NOT NULL,
        data_json TEXT NOT NULL,
        fetched_at TEXT NOT NULL DEFAULT (now()),
        PRIMARY KEY (indicator, date)
    );

    CREATE TABLE IF NOT EXISTS b3_di (
        trade_date TEXT NOT NULL,
        symbol TEXT NOT NULL,
        maturity TEXT NOT NULL,
        rate DOUBLE PRECISION,
        fetched_at TEXT NOT NULL DEFAULT (now()),
        PRIMARY KEY (trade_date, symbol)
    );

    CREATE TABLE IF NOT EXISTS treasury (
        bond_code INTEGER NOT NULL,
        base_date TEXT NOT NULL,
        data_json TEXT NOT NULL,
        fetched_at TEXT NOT NULL DEFAULT (now()),
        PRIMARY KEY (bond_code, base_date)
    );
    CREATE INDEX IF NOT EXISTS treasury_base_date_idx ON treasury (base_date);

    CREATE TABLE IF NOT EXISTS meta (
        key TEXT PRIMARY KEY,
        value TEXT
    );
"""


def _is_pg() -> bool:
    return bool(DATABASE_URL)


def uses_postgres() -> bool:
    return _is_pg()


def _get_conn() -> sqlite3.Connection:
    global _conn
    with _sqlite_lock:
        if _conn is None:
            _conn = sqlite3.connect(str(DB_PATH), check_same_thread=False)
            _conn.row_factory = sqlite3.Row
            _conn.execute("PRAGMA journal_mode=WAL")
            _conn.execute("PRAGMA synchronous=NORMAL")
            _conn.execute("PRAGMA busy_timeout=30000")
    return _conn


def _get_pg_conn():
    """Thread-local psycopg connection (lazy import to keep sqlite mode dependency-free)."""
    conn = getattr(_pg_local, "conn", None)
    if conn is None:
        import psycopg
        from psycopg.rows import dict_row

        conn = psycopg.connect(
            DATABASE_URL,
            prepare_threshold=None,
            row_factory=dict_row,
        )
        _pg_local.conn = conn
    return conn


def _execute(sql: str, params: list | tuple = ()):
    if _is_pg():
        conn = _get_pg_conn()
        with conn.cursor() as cur:
            cur.execute(sql.replace("?", "%s"), list(params))
        conn.commit()
    else:
        with _sqlite_lock:
            conn = _get_conn()
            conn.execute(sql, list(params))
            conn.commit()


def execute_many(sql: str, seq: list[tuple[str | int | float | bool | None, ...]]) -> None:
    if _is_pg():
        import psycopg

        conn = _get_pg_conn()
        try:
            with conn.cursor() as cur:
                cur.executemany(sql, seq)
            conn.commit()
        except psycopg.Error:
            conn.rollback()
            raise
    else:
        with _sqlite_lock:
            conn = _get_conn()
            try:
                conn.executemany(sql, seq)
                conn.commit()
            except sqlite3.Error:
                conn.rollback()
                raise


def execute_change(sql: str, params: tuple[object, ...]) -> int:
    if _is_pg():
        conn = _get_pg_conn()
        with conn.cursor() as cur:
            cur.execute(sql.replace("?", "%s"), params)
            changed = cur.rowcount
        conn.commit()
        return changed
    with _sqlite_lock:
        conn = _get_conn()
        cursor = conn.execute(sql, params)
        conn.commit()
        return cursor.rowcount


def _query(sql: str, params: list | tuple = ()) -> list[dict]:
    if _is_pg():
        sql = sql.replace("?", "%s")
        conn = _get_pg_conn()
        with conn.cursor() as cur:
            cur.execute(sql, list(params))
            return cur.fetchall()
    with _sqlite_lock:
        conn = _get_conn()
        rows = conn.execute(sql, list(params)).fetchall()
        return [dict(r) for r in rows]


def query_rows(
    sql: str,
    params: list[object] | tuple[object, ...] = (),
) -> list[dict[str, object]]:
    return _query(sql, params)


def source_audit_rows() -> dict[str, list[dict[str, object]]]:
    """Read latest observations and identity-aware coverage in one read-only snapshot."""
    if _is_pg():
        focus_identity = "COALESCE(data_json::jsonb->>'Indicador',''), COALESCE(data_json::jsonb->>'DataReferencia',''), COALESCE(data_json::jsonb->>'baseCalculo',''), COALESCE(data_json::jsonb->>'Suavizada','')"
        vintage_identity = "COALESCE(raw_value_json::jsonb->>'Indicador',''), COALESCE(raw_value_json::jsonb->>'DataReferencia',''), COALESCE(raw_value_json::jsonb->>'baseCalculo',''), COALESCE(raw_value_json::jsonb->>'Suavizada','')"
        focus_valid = "jsonb_typeof(data_json::jsonb->'Mediana') = 'number'"
        vintage_valid = "jsonb_typeof(raw_value_json::jsonb->'Mediana') = 'number'"
        utc_date = "(CURRENT_TIMESTAMP AT TIME ZONE 'UTC')::date"
        vintage_utc_date = "to_char(CURRENT_TIMESTAMP AT TIME ZONE 'UTC','YYYY-MM-DD')"
        snapshot_clock = "to_char(CURRENT_TIMESTAMP AT TIME ZONE 'UTC','YYYY-MM-DD\"T\"HH24:MI:SS.US\"Z\"')"
    else:
        focus_identity = "COALESCE(json_extract(data_json,'$.Indicador'),''), COALESCE(json_extract(data_json,'$.DataReferencia'),''), COALESCE(json_extract(data_json,'$.baseCalculo'),''), COALESCE(json_extract(data_json,'$.Suavizada'),'')"
        vintage_identity = "COALESCE(json_extract(raw_value_json,'$.Indicador'),''), COALESCE(json_extract(raw_value_json,'$.DataReferencia'),''), COALESCE(json_extract(raw_value_json,'$.baseCalculo'),''), COALESCE(json_extract(raw_value_json,'$.Suavizada'),'')"
        focus_valid = "json_type(data_json,'$.Mediana') IN ('integer','real')"
        vintage_valid = "json_type(raw_value_json,'$.Mediana') IN ('integer','real')"
        utc_date = "date('now')"
        vintage_utc_date = utc_date
        snapshot_clock = "strftime('%Y-%m-%dT%H:%M:%fZ','now')"
    statements = {
        "sgs": """SELECT series_code, date, value, fetched_at FROM (
            SELECT series_code, date, value, fetched_at,
                   ROW_NUMBER() OVER (PARTITION BY series_code ORDER BY date DESC, fetched_at DESC) AS rn
            FROM sgs
        ) latest WHERE rn = 1 ORDER BY series_code""",
        "focus": f"""WITH identity_latest AS (
            SELECT indicator, date, data_json, fetched_at,
                   ROW_NUMBER() OVER (PARTITION BY indicator, {focus_identity} ORDER BY date DESC, fetched_at DESC) AS identity_rank
            FROM focus_forecasts WHERE {focus_valid} AND date <= {utc_date}
        ), bounded AS (
            SELECT indicator, date, data_json, fetched_at,
                   ROW_NUMBER() OVER (PARTITION BY indicator ORDER BY date DESC, fetched_at DESC) AS cap_rank
            FROM identity_latest WHERE identity_rank = 1
        ) SELECT indicator, date, data_json, fetched_at FROM bounded WHERE cap_rank <= 200
          ORDER BY indicator, date DESC""",
        "focus_vintages": f"""WITH identity_latest AS (
            SELECT provider, series_key, reference_period, raw_value_json, available_at, payload_hash,
                   ROW_NUMBER() OVER (PARTITION BY series_key, {vintage_identity}
                                      ORDER BY reference_period DESC, available_at DESC, id DESC) AS identity_rank
            FROM observation_vintages WHERE provider = 'focus' AND {vintage_valid} AND reference_period <= {vintage_utc_date}
        ), bounded AS (
            SELECT provider, series_key, reference_period, raw_value_json, available_at, payload_hash,
                   ROW_NUMBER() OVER (PARTITION BY series_key ORDER BY reference_period DESC, available_at DESC) AS cap_rank
            FROM identity_latest WHERE identity_rank = 1
        ) SELECT provider, series_key, reference_period, raw_value_json, available_at, payload_hash
          FROM bounded WHERE cap_rank <= 200 ORDER BY series_key, reference_period DESC""",
        "audit_limits": f"""WITH focus_latest AS (
                SELECT indicator, {focus_identity}, ROW_NUMBER() OVER (
                    PARTITION BY indicator, {focus_identity} ORDER BY date DESC, fetched_at DESC) AS identity_rank
                FROM focus_forecasts WHERE {focus_valid} AND date <= {utc_date}
            ), vintage_latest AS (
                SELECT series_key, {vintage_identity}, ROW_NUMBER() OVER (
                    PARTITION BY series_key, {vintage_identity}
                    ORDER BY reference_period DESC, available_at DESC, id DESC) AS identity_rank
                FROM observation_vintages WHERE provider = 'focus' AND {vintage_valid} AND reference_period <= {vintage_utc_date}
            ) SELECT
                (SELECT CAST(COALESCE(SUM(identities - 200),0) AS INTEGER) FROM (SELECT indicator, COUNT(*) AS identities
                    FROM focus_latest WHERE identity_rank = 1 GROUP BY indicator HAVING COUNT(*) > 200) q) AS focus_current_identity_omitted,
                (SELECT CAST(COALESCE(SUM(identities - 200),0) AS INTEGER) FROM (SELECT series_key, COUNT(*) AS identities
                    FROM vintage_latest WHERE identity_rank = 1 GROUP BY series_key HAVING COUNT(*) > 200) q) AS focus_vintage_identity_omitted,
                (SELECT COUNT(*) FROM focus_latest WHERE identity_rank > 1) AS focus_history_rows_omitted,
                (SELECT COUNT(*) FROM vintage_latest WHERE identity_rank > 1) AS focus_vintage_history_rows_omitted,
                (SELECT COUNT(*) - COUNT(DISTINCT sheet_name) FROM anbima) AS anbima_history_rows_omitted,
                (SELECT COUNT(DISTINCT sheet_name) FROM anbima) AS anbima_latest_worksheets_count,
                0 AS anbima_latest_series_omitted,
                 (SELECT COUNT(*) FROM focus_forecasts WHERE date > {utc_date}) AS focus_future_rows_excluded,
                 (SELECT MAX(date) FROM focus_forecasts WHERE date > {utc_date}) AS focus_latest_future_date,
                (SELECT COUNT(*) FROM observation_vintages WHERE provider = 'focus' AND reference_period > {vintage_utc_date}) AS focus_vintage_future_rows_excluded,
                (SELECT MAX(reference_period) FROM observation_vintages WHERE provider = 'focus' AND reference_period > {vintage_utc_date}) AS focus_vintage_latest_future_date,
                {snapshot_clock} AS database_snapshot_at""",
        "sidra": """SELECT dataset_key, table_id, variable_id, classification_id, category_id,
            territory_level, territory_code, reference_period, value, unit, base_version,
            source_published_at, first_seen_at, available_at, payload_hash FROM (
            SELECT sidra_observations.*,
                   ROW_NUMBER() OVER (PARTITION BY dataset_key ORDER BY reference_period DESC, available_at DESC, id DESC) AS rn
            FROM sidra_observations
        ) latest WHERE rn = 1 ORDER BY dataset_key""",
        "b3_di": """SELECT trade_date, symbol, maturity, rate, fetched_at FROM b3_di
            WHERE trade_date = (SELECT MAX(trade_date) FROM b3_di) ORDER BY symbol""",
        "b3_identity": """SELECT snapshot_date, report_date, symbol, security_id, fin_instrm_id,
            maturity, trading_start, trading_end, source_hash, source_name, available_at, source_basis
            FROM b3_di_instrument_snapshots WHERE snapshot_date = (SELECT MAX(snapshot_date) FROM b3_di_instrument_snapshots)
            ORDER BY symbol""",
        "anbima": """SELECT sheet_name, row_index, date, data_json, fetched_at FROM (
            SELECT sheet_name, row_index, date, data_json, fetched_at,
                   ROW_NUMBER() OVER (PARTITION BY sheet_name ORDER BY date DESC, row_index DESC) AS rn
            FROM anbima
        ) latest WHERE rn = 1 ORDER BY sheet_name""",
        "treasury": """SELECT bond_code, base_date, data_json, fetched_at FROM (
            SELECT bond_code, base_date, data_json, fetched_at,
                   ROW_NUMBER() OVER (PARTITION BY bond_code ORDER BY base_date DESC, fetched_at DESC) AS rn
            FROM treasury
        ) latest WHERE rn = 1 ORDER BY bond_code""",
        "source_extractions": """SELECT extraction_id, source, indicator, qualifiers_json, status,
            network_called, started_at, finished_at, extracted_at, source_data_date,
            source_reference_min, source_reference_max, received_count, valid_count,
            persisted_count, inserted_count, updated_count, request_count, retry_count,
            run_id, task_key, cycle_id, error_code
            FROM source_extractions ORDER BY started_at DESC, extraction_id DESC LIMIT 300""",
    }
    if _is_pg():
        import psycopg
        from psycopg.rows import dict_row

        with (
            psycopg.connect(DATABASE_URL, prepare_threshold=None, row_factory=dict_row) as conn,
            conn.transaction(),
            conn.cursor() as cursor,
        ):
            cursor.execute("SET TRANSACTION ISOLATION LEVEL REPEATABLE READ READ ONLY")
            result: dict[str, list[dict[str, object]]] = {}
            for name, sql in statements.items():
                if name == "audit_limits":
                    continue
                cursor.execute(sql)
                result[name] = cursor.fetchall()
            # Mark limits based on total row counts, not on the truncated slices.
            cursor.execute(statements["audit_limits"])
            result["audit_limits"] = cursor.fetchall()
            return result
    with _sqlite_lock:
        conn = _get_conn()
        conn.execute("BEGIN")
        try:
            result = {
                name: [dict(row) for row in conn.execute(sql).fetchall()]
                for name, sql in statements.items() if name != "audit_limits"
            }
            result["audit_limits"] = [dict(conn.execute(statements["audit_limits"]).fetchone())]
            conn.commit()
            return result
        except sqlite3.Error:
            conn.rollback()
            raise


def init_db():
    if _is_pg():
        conn = _get_pg_conn()
        conn.commit()
        with conn.transaction():
            with conn.cursor() as cur:
                cur.execute(
                    "SELECT pg_advisory_xact_lock(%s)",
                    (POSTGRES_SCHEMA_LOCK_KEY,),
                )
                cur.execute(PG_SCHEMA)
            run_migrations(conn, postgres=True)
    else:
        conn = _get_conn()
        conn.executescript(SQLITE_SCHEMA)
        conn.commit()
        run_migrations(conn, postgres=False)


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _canonical_json(value) -> str:
    return json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        default=str,
        allow_nan=False,
    )


def _payload_hash(value) -> str:
    return hashlib.sha256(_canonical_json(value).encode("utf-8")).hexdigest()


def _vintage_row(
    provider: str,
    series_key: str,
    reference_period: str,
    raw_value,
    dimensions: dict | None = None,
    source_published_at: str | None = None,
    first_seen_at: str | None = None,
) -> tuple:
    dimension_values = dimensions or {}
    dimensions_json = _canonical_json(dimension_values)
    dimensions_hash = hashlib.sha256(dimensions_json.encode("utf-8")).hexdigest()
    raw_value_json = _canonical_json(raw_value)
    payload_hash = _payload_hash(raw_value)
    seen_at = first_seen_at or _utc_now()
    available_at = source_published_at or seen_at
    return (
        provider,
        series_key,
        reference_period,
        dimensions_json,
        dimensions_hash,
        raw_value_json,
        source_published_at,
        seen_at,
        available_at,
        payload_hash,
        payload_hash[:16],
    )


def _persist_current_and_vintages(
    current_sqlite: str,
    current_postgres: str,
    current_rows: list[tuple],
    vintage_rows: list[tuple],
) -> None:
    vintage_sqlite = (
        "INSERT OR IGNORE INTO observation_vintages "
        "(provider, series_key, reference_period, dimensions_json, dimensions_hash, raw_value_json, "
        "source_published_at, first_seen_at, available_at, payload_hash, revision_id) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)"
    )
    vintage_postgres = (
        "INSERT INTO observation_vintages "
        "(provider, series_key, reference_period, dimensions_json, dimensions_hash, raw_value_json, "
        "source_published_at, first_seen_at, available_at, payload_hash, revision_id) "
        "VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s) "
        "ON CONFLICT (provider, series_key, reference_period, dimensions_hash, payload_hash) DO NOTHING"
    )
    if _is_pg():
        conn = _get_pg_conn()
        try:
            with conn.cursor() as cur:
                cur.executemany(vintage_postgres, vintage_rows)
                cur.executemany(current_postgres, current_rows)
            conn.commit()
        except Exception:
            conn.rollback()
            raise
        return
    with _sqlite_lock:
        conn = _get_conn()
        try:
            conn.executemany(vintage_sqlite, vintage_rows)
            conn.executemany(current_sqlite, current_rows)
            conn.commit()
        except sqlite3.Error:
            conn.rollback()
            raise


def _parse_sgs_date(d: str) -> str:
    day, month, year = (int(part) for part in d.split("/"))
    return date(year, month, day).isoformat()


def _parse_iso_date(value: object) -> str | None:
    if not isinstance(value, str) or not value:
        return None
    try:
        return date.fromisoformat(value[:10]).isoformat()
    except ValueError:
        return None


def _finite_float(value: object) -> float | None:
    if isinstance(value, bool):
        return None
    try:
        parsed = float(str(value).replace(",", "."))
    except (TypeError, ValueError):
        return None
    return parsed if isfinite(parsed) else None


def _to_sgs_date(iso: str) -> str:
    d = date.fromisoformat(iso)
    return d.strftime("%d/%m/%Y")


def _normalize_sgs_date_bound(value: str) -> str:
    """Convert an ISO or SGS date bound to the ISO format stored in `sgs.date`."""
    try:
        parsed = date.fromisoformat(value)
    except ValueError:
        try:
            parsed = datetime.strptime(value, "%d/%m/%Y").replace(tzinfo=timezone.utc).date()
        except ValueError as exc:
            raise ValueError("SGS date bounds must be YYYY-MM-DD or DD/MM/YYYY") from exc
        if parsed.strftime("%d/%m/%Y") != value:
            raise ValueError("SGS date bounds must be YYYY-MM-DD or DD/MM/YYYY")
        return parsed.isoformat()
    if parsed.isoformat() != value:
        raise ValueError("SGS date bounds must be YYYY-MM-DD or DD/MM/YYYY")
    return parsed.isoformat()


def upsert_sgs(series_code: int, records: list[dict]):
    rows = []
    for r in records:
        try:
            iso_date = _parse_sgs_date(r["data"])
        except (KeyError, TypeError, ValueError):
            continue
        value = _finite_float(r.get("valor"))
        if value is not None:
            rows.append((series_code, iso_date, value))
    if not rows:
        return
    from indicator_catalog import INDICATOR_CATALOG

    series_key = next(
        (str(spec.key) for spec in INDICATOR_CATALOG.values() if int(spec.provider_code) == series_code),
        str(series_code),
    )
    vintage_rows = [
        _vintage_row("bcb_sgs", series_key, iso_date, {"valor": value})
        for _, iso_date, value in rows
    ]
    _persist_current_and_vintages(
        "INSERT INTO sgs (series_code, date, value) VALUES (?, ?, ?) "
        "ON CONFLICT (series_code, date) DO UPDATE SET value = excluded.value",
        "INSERT INTO sgs (series_code, date, value) VALUES (%s, %s, %s) "
        "ON CONFLICT (series_code, date) DO UPDATE SET value = EXCLUDED.value",
        rows,
        vintage_rows,
    )


def query_sgs(
    series_code: int,
    start_date: str | None = None,
    end_date: str | None = None,
) -> list[Record]:
    normalized_start = _normalize_sgs_date_bound(start_date) if start_date else None
    normalized_end = _normalize_sgs_date_bound(end_date) if end_date else None
    if normalized_start and normalized_end and normalized_start > normalized_end:
        raise ValueError("SGS start date must be on or before end date")
    sql = "SELECT date, value FROM sgs WHERE series_code = ?"
    params: list = [series_code]
    if normalized_start:
        sql += " AND date >= ?"
        params.append(normalized_start)
    if normalized_end:
        sql += " AND date <= ?"
        params.append(normalized_end)
    sql += " ORDER BY date ASC"
    rows = _query(sql, params)
    return [{"data": _to_sgs_date(r["date"]), "valor": str(r["value"])} for r in rows]


def query_sgs_latest(series_code: int, n: int = 1) -> list[Record]:
    rows = _query(
        "SELECT date, value FROM sgs WHERE series_code = ? ORDER BY date DESC LIMIT ?",
        (series_code, n),
    )
    return [{"data": _to_sgs_date(r["date"]), "valor": str(r["value"])} for r in reversed(rows)]


def get_sgs_range(series_code: int) -> tuple[str | None, str | None]:
    rows = _query(
        "SELECT MIN(date) AS min_date, MAX(date) AS max_date FROM sgs WHERE series_code = ?",
        (series_code,),
    )
    if rows and rows[0]["min_date"]:
        return rows[0]["min_date"], rows[0]["max_date"]
    return None, None


def upsert_anbima(sheet_name: str, records: list[dict]):
    rows = []
    for i, rec in enumerate(records):
        date_val = rec.get("Data de Referência") or rec.get("data") or None
        try:
            payload = _canonical_json(rec)
        except (TypeError, ValueError):
            continue
        rows.append((sheet_name, i, date_val, payload))
    if _is_pg():
        execute_many(
            "INSERT INTO anbima (sheet_name, row_index, date, data_json) VALUES (%s, %s, %s, %s) "
            "ON CONFLICT (sheet_name, row_index) DO UPDATE SET date = EXCLUDED.date, data_json = EXCLUDED.data_json",
            rows,
        )
    else:
        execute_many(
            "INSERT OR REPLACE INTO anbima (sheet_name, row_index, date, data_json) VALUES (?, ?, ?, ?)",
            rows,
        )


def query_anbima(sheet_name: str | None = None) -> dict[str, list[dict]]:
    if sheet_name:
        rows = _query(
            "SELECT data_json FROM anbima WHERE sheet_name = ? ORDER BY row_index",
            (sheet_name,),
        )
        return {sheet_name: [json.loads(r["data_json"]) for r in rows]}
    sheets = _query("SELECT DISTINCT sheet_name FROM anbima")
    result = {}
    for s in sheets:
        name = s["sheet_name"]
        rows = _query(
            "SELECT data_json FROM anbima WHERE sheet_name = ? ORDER BY row_index",
            (name,),
        )
        result[name] = [json.loads(r["data_json"]) for r in rows]
    return result


def upsert_focus(indicator: str, records: list[dict]):
    rows = []
    for rec in records:
        iso = _parse_iso_date(rec.get("Data"))
        if iso is None:
            continue
        try:
            payload = _canonical_json(rec)
        except (TypeError, ValueError):
            continue
        identity = "|".join(
            str(rec.get(key) if rec.get(key) is not None else "")
            for key in ("DataReferencia", "baseCalculo", "Suavizada")
        )
        rows.append((indicator, iso, identity, payload))
    if not rows:
        return
    vintage_rows = [
        _vintage_row("focus", indicator, iso, json.loads(payload))
        for _, iso, _, payload in rows
    ]
    _persist_current_and_vintages(
        "INSERT INTO focus_forecasts (indicator, date, identity_key, data_json) VALUES (?, ?, ?, ?) "
        "ON CONFLICT (indicator, date, identity_key) DO UPDATE SET data_json = excluded.data_json, "
        "fetched_at = strftime('%Y-%m-%dT%H:%M:%fZ','now')",
        "INSERT INTO focus_forecasts (indicator, date, identity_key, data_json) VALUES (%s, %s, %s, %s) "
        "ON CONFLICT (indicator, date, identity_key) DO UPDATE SET data_json = EXCLUDED.data_json, "
        "fetched_at = to_char(CURRENT_TIMESTAMP AT TIME ZONE 'UTC','YYYY-MM-DD\"T\"HH24:MI:SS.US\"Z\"')",
        rows,
        vintage_rows,
    )


def query_focus(indicator: str | None = None) -> dict[str, list[dict]]:
    if indicator:
        rows = _query(
            "SELECT data_json FROM focus_forecasts WHERE indicator = ? ORDER BY date, identity_key",
            (indicator,),
        )
        return {indicator: [json.loads(r["data_json"]) for r in rows]}
    indicators = _query("SELECT DISTINCT indicator FROM focus_forecasts")
    result = {}
    for ind in indicators:
        name = ind["indicator"]
        rows = _query(
            "SELECT data_json FROM focus_forecasts WHERE indicator = ? ORDER BY date, identity_key",
            (name,),
        )
        result[name] = [json.loads(r["data_json"]) for r in rows]
    return result


def upsert_b3_di(records: list[dict]):
    rows = []
    for r in records:
        trade_date = _parse_iso_date(r.get("trade_date"))
        maturity = _parse_iso_date(r.get("maturity"))
        symbol = r.get("symbol")
        rate = _finite_float(r.get("rate"))
        if trade_date is None or maturity is None or not isinstance(symbol, str) or not symbol or rate is None:
            continue
        rows.append((trade_date, symbol, maturity, rate))
    if not rows:
        return
    vintage_rows = [
        _vintage_row(
            "b3",
            "di1",
            trade_date,
            {"rate": rate},
            {"symbol": symbol, "maturity": maturity},
        )
        for trade_date, symbol, maturity, rate in rows
    ]
    _persist_current_and_vintages(
        "INSERT INTO b3_di (trade_date, symbol, maturity, rate) VALUES (?, ?, ?, ?) "
        "ON CONFLICT (trade_date, symbol) DO UPDATE SET maturity = excluded.maturity, rate = excluded.rate",
        "INSERT INTO b3_di (trade_date, symbol, maturity, rate) VALUES (%s, %s, %s, %s) "
        "ON CONFLICT (trade_date, symbol) DO UPDATE SET maturity = EXCLUDED.maturity, rate = EXCLUDED.rate",
        rows,
        vintage_rows,
    )


def query_b3_di(start_date: str | None = None, end_date: str | None = None) -> list[dict]:
    sql = "SELECT trade_date, symbol, maturity, rate FROM b3_di"
    params: list = []
    if start_date:
        sql += " WHERE trade_date >= ?"
        params.append(start_date)
    if end_date:
        sql += " AND trade_date <= ?" if params else " WHERE trade_date <= ?"
        params.append(end_date)
    sql += " ORDER BY trade_date ASC, maturity ASC"
    return _query(sql, params)


def get_b3_di_dates() -> list[str]:
    rows = _query("SELECT DISTINCT trade_date FROM b3_di ORDER BY trade_date")
    return [r["trade_date"] for r in rows]


def replace_treasury_rows(records: list[dict]) -> int:
    rows: list[tuple[int, str, str]] = []
    for record in records:
        bond_code = record.get("_bond_code")
        base_date = record.get("Data Base", "")
        if not isinstance(bond_code, int) or not base_date:
            continue
        try:
            iso_date = _parse_sgs_date(base_date)
        except (IndexError, ValueError):
            continue
        payload = {key: value for key, value in record.items() if key != "_bond_code"}
        try:
            serialized = _canonical_json(payload)
        except (TypeError, ValueError):
            continue
        rows.append((bond_code, iso_date, serialized))

    if not rows:
        return 0

    if _is_pg():
        conn = _get_pg_conn()
        with conn.cursor() as cur:
            cur.execute("DELETE FROM treasury")
            cur.executemany(
                "INSERT INTO treasury (bond_code, base_date, data_json) VALUES (%s, %s, %s)",
                rows,
            )
        conn.commit()
    else:
        conn = _get_conn()
        conn.execute("DELETE FROM treasury")
        conn.executemany(
            "INSERT INTO treasury (bond_code, base_date, data_json) VALUES (?, ?, ?)",
            rows,
        )
        conn.commit()
    return len(rows)


def query_treasury_rows() -> list[dict]:
    rows = _query("SELECT data_json FROM treasury ORDER BY base_date ASC, bond_code ASC")
    return [json.loads(row["data_json"]) for row in rows]


def query_treasury_latest_rows() -> list[dict]:
    rows = _query(
        "SELECT data_json FROM treasury "
        "WHERE base_date = (SELECT MAX(base_date) FROM treasury) "
        "ORDER BY bond_code ASC"
    )
    return [json.loads(row["data_json"]) for row in rows]


def set_meta(key: str, value: str):
    if _is_pg():
        _execute(
            "INSERT INTO meta (key, value) VALUES (%s, %s) "
            "ON CONFLICT (key) DO UPDATE SET value = EXCLUDED.value",
            (key, value),
        )
    else:
        _execute("INSERT OR REPLACE INTO meta (key, value) VALUES (?, ?)", (key, value))


def get_meta(key: str) -> str | None:
    rows = _query("SELECT value FROM meta WHERE key = ?", (key,))
    return rows[0]["value"] if rows else None


def start_source_extraction(record: dict[str, object]) -> None:
    """Persist the start of a provider operation before network or parsing."""
    try:
        _query("SELECT 1 FROM source_extractions LIMIT 1")
    except Exception as error:
        if "no such table" not in str(error).lower() and "does not exist" not in str(error).lower():
            raise
        init_db()
    fields = (
        "extraction_id", "source", "indicator", "qualifiers_json", "status",
        "network_called", "started_at", "run_id", "task_key", "cycle_id",
    )
    values = tuple(record.get(field) for field in fields)
    placeholders = ", ".join("?" for _ in fields)
    _execute(
        f"INSERT INTO source_extractions ({', '.join(fields)}) VALUES ({placeholders})",
        values,
    )


def finish_source_extraction(record: dict[str, object]) -> None:
    """Finalize one running attempt; terminal attempts are never rewritten."""
    fields = (
        "status", "network_called", "finished_at", "extracted_at", "source_data_date",
        "source_reference_min", "source_reference_max", "received_count", "valid_count",
        "persisted_count", "inserted_count", "updated_count", "request_count",
        "retry_count", "error_code", "error_detail",
    )
    assignments = ", ".join(f"{field} = ?" for field in fields)
    values = tuple(record.get(field) for field in fields) + (record["extraction_id"],)
    _execute(
        f"UPDATE source_extractions SET {assignments} "
        "WHERE extraction_id = ? AND status = 'running'",
        values,
    )


def latest_source_extractions(limit: int = 300) -> list[dict[str, object]]:
    bounded_limit = max(1, min(limit, 1000))
    return _query(
        "SELECT * FROM source_extractions ORDER BY started_at DESC, extraction_id DESC LIMIT ?",
        (bounded_limit,),
    )


def query_observation_vintages(provider: str, series_key: str) -> list[dict]:
    return _query(
        "SELECT provider, series_key, reference_period, dimensions_json, raw_value_json, "
        "source_published_at, first_seen_at, available_at, payload_hash, revision_id "
        "FROM observation_vintages WHERE provider = ? AND series_key = ? "
        "ORDER BY reference_period, available_at, first_seen_at, payload_hash",
        (provider, series_key),
    )


def insert_sidra_observations(
    observations: list[dict[str, object]],
    *,
    first_seen_at: str | None = None,
) -> None:
    from db.sidra_store import insert_sidra_observations as insert

    insert(observations, first_seen_at=first_seen_at)


def query_sidra_history(dataset_key: str) -> list[dict[str, object]]:
    from db.sidra_store import query_sidra_history as query

    return query(dataset_key)


def query_sidra_current(
    dataset_keys: tuple[str, ...] | list[str],
    *,
    as_of: str | None = None,
) -> dict[str, list[dict[str, object]]]:
    from db.sidra_store import query_sidra_current as query

    return query(dataset_keys, as_of=as_of)


def record_refresh_task_start(
    task: str,
    provider: str,
    series_expected: int,
    started_at: str,
) -> None:
    if _is_pg():
        _execute(
            "INSERT INTO refresh_task_status "
            "(task, provider, started_at, last_attempt, status, series_expected) "
            "VALUES (%s, %s, %s, %s, 'running', %s) "
            "ON CONFLICT (task) DO UPDATE SET provider = EXCLUDED.provider, started_at = EXCLUDED.started_at, "
            "last_attempt = EXCLUDED.last_attempt, status = 'running', series_expected = EXCLUDED.series_expected, "
            "series_updated = 0, record_count = 0, duration_ms = 0, error = NULL",
            (task, provider, started_at, started_at, series_expected),
        )
        return
    _execute(
        "INSERT INTO refresh_task_status "
        "(task, provider, started_at, last_attempt, status, series_expected) "
        "VALUES (?, ?, ?, ?, 'running', ?) "
        "ON CONFLICT (task) DO UPDATE SET provider = excluded.provider, started_at = excluded.started_at, "
        "last_attempt = excluded.last_attempt, status = 'running', series_expected = excluded.series_expected, "
        "series_updated = 0, record_count = 0, duration_ms = 0, error = NULL",
        (task, provider, started_at, started_at, series_expected),
    )


def record_refresh_task_finish(
    task: str,
    status: str,
    series_updated: int,
    record_count: int,
    duration_ms: int,
    error_code: str | None,
    finished_at: str,
) -> None:
    last_success = finished_at if series_updated > 0 and status in {"ok", "partial"} else None
    current = _query("SELECT last_success FROM refresh_task_status WHERE task = ?", (task,))
    preserved_success = last_success or (current[0]["last_success"] if current else None)
    _execute(
        "UPDATE refresh_task_status SET finished_at = ?, last_success = ?, status = ?, "
        "series_updated = ?, record_count = ?, duration_ms = ?, error = ? WHERE task = ?",
        (finished_at, preserved_success, status, series_updated, record_count, duration_ms, error_code, task),
    )


def list_refresh_tasks() -> list[dict[str, object]]:
    return _query(
        "SELECT task, provider, started_at, finished_at, last_attempt, last_success, status, "
        "series_expected, series_updated, record_count, duration_ms, error "
        "FROM refresh_task_status ORDER BY task"
    )


def acquire_refresh_lease(lock_key: str, owner: str, expires_at: str, now: str) -> bool:
    if _is_pg():
        conn = _get_pg_conn()
        with conn.cursor() as cur:
            cur.execute(
                "INSERT INTO refresh_leases (lock_key, owner, expires_at) VALUES (%s, %s, %s) "
                "ON CONFLICT (lock_key) DO UPDATE SET owner = EXCLUDED.owner, expires_at = EXCLUDED.expires_at "
                "WHERE refresh_leases.expires_at <= %s",
                (lock_key, owner, expires_at, now),
            )
            acquired = cur.rowcount == 1
        conn.commit()
        return acquired
    with _sqlite_lock:
        conn = _get_conn()
        cur = conn.execute(
            "INSERT INTO refresh_leases (lock_key, owner, expires_at) VALUES (?, ?, ?) "
            "ON CONFLICT (lock_key) DO UPDATE SET owner = excluded.owner, expires_at = excluded.expires_at "
            "WHERE refresh_leases.expires_at <= ?",
            (lock_key, owner, expires_at, now),
        )
        conn.commit()
        return cur.rowcount == 1


def heartbeat_refresh_lease(lock_key: str, owner: str, expires_at: str) -> bool:
    if _is_pg():
        conn = _get_pg_conn()
        with conn.cursor() as cur:
            cur.execute(
                "UPDATE refresh_leases SET expires_at = %s WHERE lock_key = %s AND owner = %s",
                (expires_at, lock_key, owner),
            )
            updated = cur.rowcount == 1
        conn.commit()
        return updated
    with _sqlite_lock:
        conn = _get_conn()
        cur = conn.execute(
            "UPDATE refresh_leases SET expires_at = ? WHERE lock_key = ? AND owner = ?",
            (expires_at, lock_key, owner),
        )
        conn.commit()
        return cur.rowcount == 1


def release_refresh_lease(lock_key: str, owner: str) -> bool:
    if _is_pg():
        conn = _get_pg_conn()
        with conn.cursor() as cur:
            cur.execute("DELETE FROM refresh_leases WHERE lock_key = %s AND owner = %s", (lock_key, owner))
            released = cur.rowcount == 1
        conn.commit()
        return released
    with _sqlite_lock:
        conn = _get_conn()
        cur = conn.execute("DELETE FROM refresh_leases WHERE lock_key = ? AND owner = ?", (lock_key, owner))
        conn.commit()
        return cur.rowcount == 1


def db_stats() -> dict:
    stats = {}
    for name, table in (
        ("sgs", "sgs"),
        ("anbima", "anbima"),
        ("focus", None),
        ("b3_di", "b3_di"),
        ("treasury", "treasury"),
        ("sidra_observations", "sidra_observations"),
    ):
        if name == "focus":
            stats[name] = _focus_observation_count()
            continue
        rows = _query(f"SELECT COUNT(*) AS c FROM {table}")
        stats[name] = rows[0]["c"] if rows else 0
    return stats


def _focus_observation_count() -> int:
    """Count distinct forecast identities across canonical and legacy storage.

    Legacy ``focus`` rows are one forecast JSON object per indicator/date. The
    union prevents migration copies from being counted twice, while retaining
    legacy-only identities during compatibility periods.
    """
    if _is_pg():
        legacy_date = "date::text"
        legacy_identity = (
            "COALESCE(data_json::jsonb ->> 'DataReferencia', '') || '|' || "
            "COALESCE(data_json::jsonb ->> 'baseCalculo', '') || '|' || "
            "COALESCE(data_json::jsonb ->> 'Suavizada', '')"
        )
    else:
        legacy_date = "date"
        legacy_identity = (
            "COALESCE(CAST(json_extract(data_json, '$.DataReferencia') AS TEXT), '') || '|' || "
            "COALESCE(CAST(json_extract(data_json, '$.baseCalculo') AS TEXT), '') || '|' || "
            "COALESCE(CAST(json_extract(data_json, '$.Suavizada') AS TEXT), '')"
        )
    rows = _query(
        "SELECT COUNT(*) AS c FROM ("
        "SELECT indicator, date::text AS reference_date, identity_key FROM focus_forecasts "
        "UNION "
        f"SELECT indicator, {legacy_date} AS reference_date, {legacy_identity} AS identity_key "
        "FROM focus) AS focus_identities"
        if _is_pg()
        else "SELECT COUNT(*) AS c FROM ("
        "SELECT indicator, date AS reference_date, identity_key FROM focus_forecasts "
        "UNION "
        f"SELECT indicator, {legacy_date} AS reference_date, {legacy_identity} AS identity_key "
        "FROM focus) AS focus_identities"
    )
    return int(rows[0]["c"]) if rows else 0
