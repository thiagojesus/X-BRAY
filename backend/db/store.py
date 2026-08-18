import os
import json
import sqlite3
import threading
from datetime import date, datetime
from pathlib import Path
from config import BASE_DIR

DB_PATH = BASE_DIR / "xbry.db"
DATABASE_URL = os.environ.get("DATABASE_URL", "")

_conn: sqlite3.Connection | None = None
_pg_local = threading.local()

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

    CREATE TABLE IF NOT EXISTS meta (
        key TEXT PRIMARY KEY,
        value TEXT
    );
"""


def _is_pg() -> bool:
    return bool(DATABASE_URL)


def _get_conn() -> sqlite3.Connection:
    global _conn
    if _conn is None:
        _conn = sqlite3.connect(str(DB_PATH), check_same_thread=False)
        _conn.row_factory = sqlite3.Row
        _conn.execute("PRAGMA journal_mode=WAL")
        _conn.execute("PRAGMA synchronous=NORMAL")
    return _conn


def _get_pg_conn():
    """Thread-local psycopg connection (lazy import to keep sqlite mode dependency-free)."""
    conn = getattr(_pg_local, "conn", None)
    if conn is None:
        import psycopg
        from psycopg.rows import dict_row

        conn = psycopg.connect(DATABASE_URL, row_factory=dict_row)
        _pg_local.conn = conn
    return conn


def _execute(sql: str, params: list | tuple = ()):
    if _is_pg():
        conn = _get_pg_conn()
        with conn.cursor() as cur:
            cur.execute(sql, list(params))
        conn.commit()
    else:
        conn = _get_conn()
        conn.execute(sql, list(params))
        conn.commit()


def _execute_many(sql: str, seq: list[tuple]):
    if _is_pg():
        conn = _get_pg_conn()
        with conn.cursor() as cur:
            cur.executemany(sql, seq)
        conn.commit()
    else:
        conn = _get_conn()
        conn.executemany(sql, seq)
        conn.commit()


def _query(sql: str, params: list | tuple = ()) -> list[dict]:
    if _is_pg():
        sql = sql.replace("?", "%s")
        conn = _get_pg_conn()
        with conn.cursor() as cur:
            cur.execute(sql, list(params))
            return cur.fetchall()
    conn = _get_conn()
    rows = conn.execute(sql, list(params)).fetchall()
    return [dict(r) for r in rows]


def init_db():
    if _is_pg():
        conn = _get_pg_conn()
        with conn.cursor() as cur:
            cur.execute(PG_SCHEMA)
        conn.commit()
    else:
        conn = _get_conn()
        conn.executescript(SQLITE_SCHEMA)
        conn.commit()


def _parse_sgs_date(d: str) -> str:
    parts = d.split("/")
    return f"{parts[2]}-{parts[1].zfill(2)}-{parts[0].zfill(2)}"


def _to_sgs_date(iso: str) -> str:
    d = date.fromisoformat(iso)
    return d.strftime("%d/%m/%Y")


def upsert_sgs(series_code: int, records: list[dict]):
    rows = []
    for r in records:
        try:
            iso_date = _parse_sgs_date(r["data"])
            val = float(str(r["valor"]).replace(",", "."))
            rows.append((series_code, iso_date, val))
        except Exception:
            continue
    if not rows:
        return
    if _is_pg():
        _execute_many(
            "INSERT INTO sgs (series_code, date, value) VALUES (%s, %s, %s) "
            "ON CONFLICT (series_code, date) DO UPDATE SET value = EXCLUDED.value",
            rows,
        )
    else:
        _execute_many(
            "INSERT OR REPLACE INTO sgs (series_code, date, value) VALUES (?, ?, ?)",
            rows,
        )


def query_sgs(series_code: int, start_date: str | None = None, end_date: str | None = None) -> list[dict]:
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


def query_sgs_latest(series_code: int, n: int = 1) -> list[dict]:
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
        rows.append((sheet_name, i, date_val, json.dumps(rec, ensure_ascii=False, default=str)))
    if _is_pg():
        _execute_many(
            "INSERT INTO anbima (sheet_name, row_index, date, data_json) VALUES (%s, %s, %s, %s) "
            "ON CONFLICT (sheet_name, row_index) DO UPDATE SET date = EXCLUDED.date, data_json = EXCLUDED.data_json",
            rows,
        )
    else:
        _execute_many(
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
        data_str = rec.get("Data", "")
        if data_str:
            try:
                iso = data_str[:10]
                if "T" in iso:
                    iso = iso.split("T")[0]
            except Exception:
                continue
        else:
            continue
        rows.append((indicator, iso, json.dumps(rec, ensure_ascii=False, default=str)))
    if _is_pg():
        _execute_many(
            "INSERT INTO focus (indicator, date, data_json) VALUES (%s, %s, %s) "
            "ON CONFLICT (indicator, date) DO UPDATE SET data_json = EXCLUDED.data_json",
            rows,
        )
    else:
        _execute_many(
            "INSERT OR REPLACE INTO focus (indicator, date, data_json) VALUES (?, ?, ?)",
            rows,
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
        rows.append((r["trade_date"], r["symbol"], r["maturity"], r["rate"]))
    if _is_pg():
        _execute_many(
            "INSERT INTO b3_di (trade_date, symbol, maturity, rate) VALUES (%s, %s, %s, %s) "
            "ON CONFLICT (trade_date, symbol) DO UPDATE SET maturity = EXCLUDED.maturity, rate = EXCLUDED.rate",
            rows,
        )
    else:
        _execute_many(
            "INSERT OR REPLACE INTO b3_di (trade_date, symbol, maturity, rate) VALUES (?, ?, ?, ?)",
            rows,
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


def db_stats() -> dict:
    stats = {}
    for table in ["sgs", "anbima", "focus", "b3_di"]:
        rows = _query(f"SELECT COUNT(*) AS c FROM {table}")
        stats[table] = rows[0]["c"] if rows else 0
    return stats