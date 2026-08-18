import sqlite3
import json
from datetime import date, datetime
from pathlib import Path
from config import BASE_DIR

DB_PATH = BASE_DIR / "xbry.db"

_conn: sqlite3.Connection | None = None


def _get_conn() -> sqlite3.Connection:
    global _conn
    if _conn is None:
        _conn = sqlite3.connect(str(DB_PATH), check_same_thread=False)
        _conn.row_factory = sqlite3.Row
        _conn.execute("PRAGMA journal_mode=WAL")
        _conn.execute("PRAGMA synchronous=NORMAL")
    return _conn


def init_db():
    conn = _get_conn()
    conn.executescript("""
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
    """)
    conn.commit()


def _parse_sgs_date(d: str) -> str:
    parts = d.split("/")
    return f"{parts[2]}-{parts[1].zfill(2)}-{parts[0].zfill(2)}"


def _to_sgs_date(iso: str) -> str:
    d = date.fromisoformat(iso)
    return d.strftime("%d/%m/%Y")


def upsert_sgs(series_code: int, records: list[dict]):
    conn = _get_conn()
    rows = []
    for r in records:
        try:
            iso_date = _parse_sgs_date(r["data"])
            val = float(str(r["valor"]).replace(",", "."))
            rows.append((series_code, iso_date, val))
        except Exception:
            continue
    conn.executemany(
        "INSERT OR REPLACE INTO sgs (series_code, date, value) VALUES (?, ?, ?)",
        rows,
    )
    conn.commit()


def query_sgs(series_code: int, start_date: str | None = None, end_date: str | None = None) -> list[dict]:
    conn = _get_conn()
    sql = "SELECT date, value FROM sgs WHERE series_code = ?"
    params: list = [series_code]
    if start_date:
        sql += " AND date >= ?"
        params.append(start_date)
    if end_date:
        sql += " AND date <= ?"
        params.append(end_date)
    sql += " ORDER BY date ASC"
    rows = conn.execute(sql, params).fetchall()
    return [{"data": _to_sgs_date(r["date"]), "valor": str(r["value"])} for r in rows]


def query_sgs_latest(series_code: int, n: int = 1) -> list[dict]:
    conn = _get_conn()
    rows = conn.execute(
        "SELECT date, value FROM sgs WHERE series_code = ? ORDER BY date DESC LIMIT ?",
        (series_code, n),
    ).fetchall()
    return [{"data": _to_sgs_date(r["date"]), "valor": str(r["value"])} for r in reversed(rows)]


def get_sgs_range(series_code: int) -> tuple[str | None, str | None]:
    conn = _get_conn()
    row = conn.execute(
        "SELECT MIN(date), MAX(date) FROM sgs WHERE series_code = ?",
        (series_code,),
    ).fetchone()
    if row and row[0]:
        return row[0], row[1]
    return None, None


def upsert_anbima(sheet_name: str, records: list[dict]):
    conn = _get_conn()
    rows = []
    for i, rec in enumerate(records):
        date_val = rec.get("Data de Referência") or rec.get("data") or None
        rows.append((sheet_name, i, date_val, json.dumps(rec, ensure_ascii=False, default=str)))
    conn.executemany(
        "INSERT OR REPLACE INTO anbima (sheet_name, row_index, date, data_json) VALUES (?, ?, ?, ?)",
        rows,
    )
    conn.commit()


def query_anbima(sheet_name: str | None = None) -> dict[str, list[dict]]:
    conn = _get_conn()
    if sheet_name:
        rows = conn.execute(
            "SELECT data_json FROM anbima WHERE sheet_name = ? ORDER BY row_index",
            (sheet_name,),
        ).fetchall()
        return {sheet_name: [json.loads(r["data_json"]) for r in rows]}
    else:
        sheets = conn.execute("SELECT DISTINCT sheet_name FROM anbima").fetchall()
        result = {}
        for s in sheets:
            name = s["sheet_name"]
            rows = conn.execute(
                "SELECT data_json FROM anbima WHERE sheet_name = ? ORDER BY row_index",
                (name,),
            ).fetchall()
            result[name] = [json.loads(r["data_json"]) for r in rows]
        return result


def upsert_focus(indicator: str, records: list[dict]):
    conn = _get_conn()
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
    conn.executemany(
        "INSERT OR REPLACE INTO focus (indicator, date, data_json) VALUES (?, ?, ?)",
        rows,
    )
    conn.commit()


def query_focus(indicator: str | None = None) -> dict[str, list[dict]]:
    conn = _get_conn()
    if indicator:
        rows = conn.execute(
            "SELECT data_json FROM focus WHERE indicator = ? ORDER BY date",
            (indicator,),
        ).fetchall()
        return {indicator: [json.loads(r["data_json"]) for r in rows]}
    else:
        indicators = conn.execute("SELECT DISTINCT indicator FROM focus").fetchall()
        result = {}
        for ind in indicators:
            name = ind["indicator"]
            rows = conn.execute(
                "SELECT data_json FROM focus WHERE indicator = ? ORDER BY date",
                (name,),
            ).fetchall()
            result[name] = [json.loads(r["data_json"]) for r in rows]
        return result


def upsert_b3_di(records: list[dict]):
    conn = _get_conn()
    rows = []
    for r in records:
        rows.append((r["trade_date"], r["symbol"], r["maturity"], r["rate"]))
    conn.executemany(
        "INSERT OR REPLACE INTO b3_di (trade_date, symbol, maturity, rate) VALUES (?, ?, ?, ?)",
        rows,
    )
    conn.commit()


def query_b3_di(start_date: str | None = None, end_date: str | None = None) -> list[dict]:
    conn = _get_conn()
    sql = "SELECT trade_date, symbol, maturity, rate FROM b3_di"
    params: list = []
    if start_date:
        sql += " WHERE trade_date >= ?"
        params.append(start_date)
    if end_date:
        sql += " AND trade_date <= ?" if params else " WHERE trade_date <= ?"
        params.append(end_date)
    sql += " ORDER BY trade_date ASC, maturity ASC"
    rows = conn.execute(sql, params).fetchall()
    return [dict(r) for r in rows]


def get_b3_di_dates() -> list[str]:
    conn = _get_conn()
    rows = conn.execute("SELECT DISTINCT trade_date FROM b3_di ORDER BY trade_date").fetchall()
    return [r["trade_date"] for r in rows]


def set_meta(key: str, value: str):
    conn = _get_conn()
    conn.execute("INSERT OR REPLACE INTO meta (key, value) VALUES (?, ?)", (key, value))
    conn.commit()


def get_meta(key: str) -> str | None:
    conn = _get_conn()
    row = conn.execute("SELECT value FROM meta WHERE key = ?", (key,)).fetchone()
    return row["value"] if row else None


def db_stats() -> dict:
    conn = _get_conn()
    stats = {}
    for table in ["sgs", "anbima", "focus", "b3_di"]:
        count = conn.execute(f"SELECT COUNT(*) as c FROM {table}").fetchone()["c"]
        stats[table] = count
    return stats
