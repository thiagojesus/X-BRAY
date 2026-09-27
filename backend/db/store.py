import os
import json
import sqlite3
import threading
import hashlib
from contextlib import contextmanager
from datetime import date, datetime, timezone
from math import isfinite
from pathlib import Path
from config import BASE_DIR
from db.migrations import run_migrations
from domain_contracts import Record

DB_PATH = BASE_DIR / "xbry.db"
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
            cur.execute(sql, list(params))
        conn.commit()
    else:
        with _sqlite_lock:
            conn = _get_conn()
            conn.execute(sql, list(params))
            conn.commit()


def execute_many(sql: str, seq: list[tuple[object, ...]]) -> None:
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


def init_db():
    if _is_pg():
        conn = _get_pg_conn()
        with conn.cursor() as cur:
            cur.execute(PG_SCHEMA)
        conn.commit()
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
    return datetime.strptime(d, "%d/%m/%Y").date().isoformat()


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
    sql = "SELECT date, value FROM sgs WHERE series_code = ?"
    params: list = [series_code]
    if start_date:
        sql += " AND date >= ?"
        params.append(start_date)
    if end_date:
        sql += " AND date <= ?"
        params.append(end_date)
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
        rows.append((indicator, iso, payload))
    if _is_pg():
        current_postgres = (
            "INSERT INTO focus (indicator, date, data_json) VALUES (%s, %s, %s) "
            "ON CONFLICT (indicator, date) DO UPDATE SET data_json = EXCLUDED.data_json"
        )
    else:
        current_postgres = ""
    if not rows:
        return
    vintage_rows = [
        _vintage_row("focus", indicator, iso, json.loads(payload))
        for _, iso, payload in rows
    ]
    _persist_current_and_vintages(
        "INSERT INTO focus (indicator, date, data_json) VALUES (?, ?, ?) "
        "ON CONFLICT (indicator, date) DO UPDATE SET data_json = excluded.data_json",
        current_postgres or (
            "INSERT INTO focus (indicator, date, data_json) VALUES (%s, %s, %s) "
            "ON CONFLICT (indicator, date) DO UPDATE SET data_json = EXCLUDED.data_json"
        ),
        rows,
        vintage_rows,
    )


def query_focus(indicator: str | None = None) -> dict[str, list[dict]]:
    if indicator:
        rows = _query(
            "SELECT data_json FROM focus WHERE indicator = ? ORDER BY date",
            (indicator,),
        )
        return {indicator: [json.loads(r["data_json"]) for r in rows]}
    indicators = _query("SELECT DISTINCT indicator FROM focus")
    result = {}
    for ind in indicators:
        name = ind["indicator"]
        rows = _query(
            "SELECT data_json FROM focus WHERE indicator = ? ORDER BY date",
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
    for table in ["sgs", "anbima", "focus", "b3_di", "treasury", "sidra_observations"]:
        rows = _query(f"SELECT COUNT(*) AS c FROM {table}")
        stats[table] = rows[0]["c"] if rows else 0
    return stats
