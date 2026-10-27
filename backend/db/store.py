import fcntl
import hashlib
import json
import os
import re
import sqlite3
import tempfile
import threading
from collections.abc import Callable, Iterator, Mapping, Sequence
from contextlib import contextmanager, suppress
from datetime import date, datetime, timezone
from math import isfinite
from pathlib import Path
from time import monotonic
from typing import NamedTuple, TypedDict, cast

from config import BASE_DIR
from db.migrations import POSTGRES_SCHEMA_LOCK_KEY, run_migrations
from domain_contracts import Record

DB_PATH = Path(os.environ.get("XBRAY_SQLITE_PATH", str(BASE_DIR / "xbry.db"))).expanduser()
DATABASE_URL = os.environ.get("DATABASE_URL", "")

_conn: sqlite3.Connection | None = None
_pg_local = threading.local()
_sqlite_lock = threading.RLock()


class CanaryBinding(NamedTuple):
    operation_id: str
    candidate_id: str
    as_of: str
    plan_sha256: str
    proof_sha256: str
    contract_version: str
    profile_semantic_sha256: str
    parser_semantic_sha256: str
    actual_reference_period: str | None
    captured_at: str
    request_count: int
    inserted_count: int
    updated_count: int
    bound_at: str


class SourceCanaryBinding(TypedDict):
    operation_id: str
    candidate_id: str
    as_of: str
    plan_sha256: str
    proof_sha256: str
    contract_version: str
    profile_semantic_sha256: str
    parser_semantic_sha256: str
    actual_reference_period: str | None
    captured_at: str
    request_count: int
    inserted_count: int
    updated_count: int
    bound_at: str


class TreasuryCohortReceipt(NamedTuple):
    extraction_id: str
    source_payload_sha256: str
    observation_count: int
    reference_min: str
    reference_max: str
    captured_at: str

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
            connect_timeout=5,
        )
        _pg_local.conn = conn
    return conn


def _execute(sql: str, params: list | tuple = ()):
    if _is_pg():
        conn = _get_pg_conn()
        try:
            with conn.cursor() as cur:
                cur.execute(sql.replace("?", "%s"), list(params))
            conn.commit()
        except Exception:
            with suppress(Exception):
                conn.rollback()
            raise
    else:
        with _sqlite_lock:
            conn = _get_conn()
            try:
                conn.execute(sql, list(params))
                conn.commit()
            except Exception:
                with suppress(Exception):
                    conn.rollback()
                raise


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


def execute_transaction(
    statements: Sequence[tuple[str, Sequence[tuple[object, ...]]]],
    *,
    commit_receipt: tuple[object, ...] | None = None,
    canary_binding: CanaryBinding | None = None,
) -> None:
    """Execute a bounded set of bulk writes in one database transaction."""
    validate_canary_receipt(canary_binding, commit_receipt)
    if _is_pg():
        conn = _get_pg_conn()
        try:
            with conn.cursor() as cur:
                from db.refresh_attempt import current_refresh_attempt
                active_attempt = current_refresh_attempt()
                if active_attempt is not None:
                    assert_current_refresh_attempt(conn, active_attempt)
                if canary_binding is not None:
                    inserted = _claim_canary_binding(cur, canary_binding, postgres=True)
                    if not inserted:
                        if commit_receipt is None or _claim_commit_receipt(
                            cur, commit_receipt, postgres=True
                        ):
                            raise RuntimeError("canary binding exists without its completed receipt")
                        conn.commit()
                        return
                if commit_receipt is not None and not _claim_commit_receipt(
                    cur, commit_receipt, postgres=True
                ):
                    conn.commit()
                    return
                for sql, rows in statements:
                    if rows:
                        cur.executemany(sql.replace("?", "%s"), rows)
            conn.commit()
        except Exception:
            with suppress(Exception):
                conn.rollback()
            raise
        return
    with _sqlite_lock:
        conn = _get_conn()
        try:
            from db.refresh_attempt import current_refresh_attempt
            active_attempt = current_refresh_attempt()
            if active_attempt is not None:
                assert_current_refresh_attempt(conn, active_attempt)
            if canary_binding is not None:
                inserted = _claim_canary_binding(conn, canary_binding, postgres=False)
                if not inserted:
                    if commit_receipt is None or _claim_commit_receipt(
                        conn, commit_receipt, postgres=False
                    ):
                        raise RuntimeError("canary binding exists without its completed receipt")
                    conn.commit()
                    return
            if commit_receipt is not None and not _claim_commit_receipt(
                conn, commit_receipt, postgres=False
            ):
                conn.commit()
                return
            for sql, rows in statements:
                if rows:
                    conn.executemany(sql, rows)
            conn.commit()
        except Exception:
            with suppress(Exception):
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


def record_refresh_attempt_started(context: object, now: str) -> None:
    values = (
        context.attempt_id, str(context.run_id), str(context.item_key),
        context.attempt_number, context.generation, str(context.claim_token),
        context.expires_at, now,
    )
    sql = (
        "INSERT INTO refresh_attempt_lifecycle "
        "(attempt_id, run_id, item_key, attempt_number, claim_generation, claim_token, "
        "claim_expires_at, started_at, last_stage) VALUES (?, ?, ?, ?, ?, ?, ?, ?, 'claimed')"
    )
    if _is_pg():
        conn = _get_pg_conn()
        with conn.cursor() as cur:
            cur.execute(sql.replace("?", "%s"), values)
            cur.execute(
                "INSERT INTO refresh_attempt_milestones (attempt_id, stage, observed_at, outcome) "
                "VALUES (%s, 'claimed', %s, 'observed')", (context.attempt_id, now),
            )
        conn.commit()
        return
    with _sqlite_lock:
        conn = _get_conn()
        conn.execute(sql, values)
        conn.execute(
            "INSERT INTO refresh_attempt_milestones (attempt_id, stage, observed_at, outcome) "
            "VALUES (?, 'claimed', ?, 'observed')", (context.attempt_id, now),
        )
        conn.commit()


def record_refresh_attempt_milestone(
    attempt_id: str, stage: str, outcome: str, detail_code: str | None,
) -> None:
    observed_at = _utc_now()
    values = (attempt_id, stage, observed_at, outcome, detail_code)
    if _is_pg():
        conn = _get_pg_conn()
        with conn.cursor() as cur:
            cur.execute(
                "INSERT INTO refresh_attempt_milestones "
                "(attempt_id, stage, observed_at, outcome, detail_code) VALUES (%s, %s, %s, %s, %s)",
                values,
            )
            cur.execute(
                "UPDATE refresh_attempt_lifecycle SET last_stage = %s, "
                "last_error_code = CASE WHEN %s <> 'observed' THEN %s ELSE last_error_code END "
                "WHERE attempt_id = %s AND terminal_at IS NULL",
                (stage, outcome, detail_code, attempt_id),
            )
        conn.commit()
        return
    with _sqlite_lock:
        conn = _get_conn()
        conn.execute(
            "INSERT INTO refresh_attempt_milestones "
            "(attempt_id, stage, observed_at, outcome, detail_code) VALUES (?, ?, ?, ?, ?)", values,
        )
        conn.execute(
            "UPDATE refresh_attempt_lifecycle SET last_stage = ?, "
            "last_error_code = CASE WHEN ? <> 'observed' THEN ? ELSE last_error_code END "
            "WHERE attempt_id = ? AND terminal_at IS NULL",
            (stage, outcome, detail_code, attempt_id),
        )
        conn.commit()


def finish_refresh_attempt(
    attempt_id: str, outcome: str, error_code: str | None,
    committed_count: int | None, source_data_date: str | None,
) -> None:
    finished_at = _utc_now()
    if _is_pg():
        conn = _get_pg_conn()
        with conn.cursor() as cur:
            cur.execute(
                "UPDATE refresh_attempt_lifecycle SET terminal_outcome = %s, terminal_at = %s, "
                "last_error_code = %s, committed_count = %s, source_data_date = %s "
                "WHERE attempt_id = %s AND terminal_at IS NULL",
                (outcome, finished_at, error_code, committed_count, source_data_date, attempt_id),
            )
        conn.commit()
    else:
        with _sqlite_lock:
            conn = _get_conn()
            conn.execute(
                "UPDATE refresh_attempt_lifecycle SET terminal_outcome = ?, terminal_at = ?, "
                "last_error_code = ?, committed_count = ?, source_data_date = ? "
                "WHERE attempt_id = ? AND terminal_at IS NULL",
                (outcome, finished_at, error_code, committed_count, source_data_date, attempt_id),
            )
            conn.commit()


def assert_current_refresh_attempt(connection: object, context: object) -> None:
    now = _utc_now()
    lock = " FOR UPDATE" if _is_pg() else ""
    if _is_pg():
        expires = datetime.fromisoformat(str(context.expires_at).replace("Z", "+00:00"))
        remaining_ms = int((expires - datetime.now(timezone.utc)).total_seconds() * 1000)
        if remaining_ms <= 0:
            raise RuntimeError("refresh claim is expired or has been replaced")
        connection.execute(
            "SELECT set_config('lock_timeout', %s, true), set_config('statement_timeout', %s, true)",
            (str(max(1, min(1000, remaining_ms))), str(max(1, remaining_ms))),
        )
    query = (
        "SELECT i.claim_token, i.attempt, i.claim_expires_at, r.state, l.owner, l.expires_at "
        "FROM refresh_work_items i JOIN refresh_runs r ON r.run_id = i.run_id "
        "JOIN refresh_leases l ON l.lock_key = 'daily_refresh' "
        "WHERE i.run_id = ? AND i.item_key = ?" + lock
    )
    cursor = connection.execute(query.replace("?", "%s") if _is_pg() else query,
                                (str(context.run_id), str(context.item_key)))
    row = cursor.fetchone()
    if row is None:
        raise RuntimeError("refresh claim is no longer current")
    values = tuple(row.values()) if isinstance(row, Mapping) else tuple(row)
    token, attempt, item_expiry, run_state, lease_owner, lease_expiry = values
    if (
        str(token) != str(context.claim_token)
        or int(attempt) != context.generation
        or str(item_expiry) <= now
        or str(run_state) != "running"
        or str(lease_owner) != str(context.run_id)
        or str(lease_expiry) <= now
    ):
        raise RuntimeError("refresh claim is expired or has been replaced")


def _claim_commit_receipt(
    cursor: object, receipt: tuple[object, ...], *, postgres: bool
) -> bool:
    """Claim an operation receipt first; exact replays are no-op, conflicts fail closed."""
    placeholders = "%s" if postgres else "?"
    insert = (
        "INSERT INTO source_acquisition_commits "
        "(extraction_id, selected_route, candidate_id, observation_count, source_payload_sha256, committed_at) "
        f"VALUES ({', '.join([placeholders] * 6)}) "
        + (
            "ON CONFLICT (extraction_id) DO NOTHING RETURNING extraction_id"
            if postgres else "ON CONFLICT (extraction_id) DO NOTHING"
        )
    )
    result = cursor.execute(insert, receipt)
    if postgres:
        if result.fetchone() is not None:
            return True
    elif result.rowcount == 1:
        return True
    result = cursor.execute(
        "SELECT selected_route, candidate_id, observation_count, source_payload_sha256 "
        f"FROM source_acquisition_commits WHERE extraction_id = {placeholders}",
        (receipt[0],),
    )
    existing = result.fetchone()
    if existing is None:
        raise RuntimeError("operation receipt conflict could not be reconciled")
    names = (
        "selected_route", "candidate_id", "observation_count", "source_payload_sha256"
    )
    actual = tuple(existing[name] for name in names) if isinstance(existing, Mapping) else tuple(existing)
    if actual != tuple(receipt[1:5]):
        raise ValueError("operation id already has a different selected batch receipt")
    return False


def _claim_canary_binding(
    cursor: object, binding: CanaryBinding, *, postgres: bool
) -> bool:
    """Bind one operation id to an immutable code-approved plan before writes."""
    if len(binding) != 14:
        raise ValueError("invalid canary binding")
    operation_id, candidate_id, as_of, plan_hash, proof_hash, contract_version, profile_hash, parser_hash, actual_period, captured_at, request_count, inserted_count, updated_count, bound_at = binding
    if not all(isinstance(value, str) and value for value in (
        operation_id, candidate_id, as_of, contract_version, captured_at, bound_at,
    )):
        raise ValueError("invalid canary binding identity")
    if actual_period is not None and (not isinstance(actual_period, str) or not actual_period):
        raise ValueError("invalid canary reference period")
    if (
        not isinstance(request_count, int) or isinstance(request_count, bool) or request_count < 0
        or not isinstance(inserted_count, int) or isinstance(inserted_count, bool) or inserted_count < 0
        or not isinstance(updated_count, int) or isinstance(updated_count, bool) or updated_count < 0
    ):
        raise ValueError("invalid canary business-write counts")
    try:
        if date.fromisoformat(as_of).isoformat() != as_of:
            raise ValueError("invalid canary binding date")
    except ValueError as exc:
        raise ValueError("invalid canary binding date") from exc
    for digest in (plan_hash, proof_hash, profile_hash, parser_hash):
        if not isinstance(digest, str) or re.fullmatch(r"[0-9a-f]{64}", digest) is None:
            raise ValueError("invalid canary binding digest")
    placeholders = "%s" if postgres else "?"
    values = binding
    insert = (
        "INSERT INTO source_canary_bindings "
        "(operation_id, candidate_id, as_of, plan_sha256, proof_sha256, contract_version, "
        "profile_semantic_sha256, parser_semantic_sha256, actual_reference_period, captured_at, request_count, inserted_count, "
        "updated_count, bound_at) "
        f"VALUES ({', '.join([placeholders] * 14)}) "
        + (
            "ON CONFLICT (operation_id) DO NOTHING RETURNING operation_id"
            if postgres else "ON CONFLICT (operation_id) DO NOTHING"
        )
    )
    result = cursor.execute(insert, values)
    if postgres:
        if result.fetchone() is not None:
            return True
    elif result.rowcount == 1:
        return True
    result = cursor.execute(
        "SELECT candidate_id, as_of, plan_sha256, proof_sha256, contract_version, "
        "profile_semantic_sha256, parser_semantic_sha256, actual_reference_period, captured_at, request_count, inserted_count, "
        "updated_count FROM source_canary_bindings "
        f"WHERE operation_id = {placeholders}",
        (operation_id,),
    )
    existing = result.fetchone()
    if existing is None:
        raise RuntimeError("canary binding conflict could not be reconciled")
    fields = (
        "candidate_id", "as_of", "plan_sha256", "proof_sha256", "contract_version",
        "profile_semantic_sha256", "parser_semantic_sha256", "actual_reference_period", "captured_at",
        "request_count", "inserted_count", "updated_count",
    )
    actual = tuple(existing[name] for name in fields) if isinstance(existing, Mapping) else tuple(existing)
    if actual != tuple(binding[1:13]):
        raise ValueError("operation id already has a different canary binding")
    return False


def validate_canary_receipt(
    binding: CanaryBinding | None,
    receipt: tuple[object, ...] | None,
) -> None:
    if binding is None:
        return
    if receipt is None or len(receipt) != 6 or len(binding) != 14:
        raise ValueError("canary business writes require an acquisition receipt")
    if receipt[0] != binding[0] or receipt[1] != "candidate" or receipt[2] != binding[1]:
        raise ValueError("canary binding does not match candidate acquisition receipt")


def validate_canary_origin_rows(
    binding: CanaryBinding | None,
    origins: Sequence[tuple[object, ...]] | None,
) -> None:
    if binding is None:
        return
    if not origins:
        raise ValueError("canary writes require attested observation origins")
    for origin in origins:
        if len(origin) != 16:
            raise ValueError("canary origin is missing semantic attestation")
        if (
            origin[1] != binding[0]
            or origin[8] != binding[1]
            or origin[12] != binding[5]
            or origin[13] != binding[6]
            or origin[14] != binding[7]
            or origin[15] != binding[4]
        ):
            raise ValueError("canary origin attestation does not match operation binding")


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
        sidra_identity_json = "json_build_object('table_id', table_id, 'variable_id', variable_id, 'classification_id', classification_id, 'category_id', category_id, 'territory_level', territory_level, 'territory_code', territory_code, 'unit', unit, 'base_version', base_version)::text"
    else:
        focus_identity = "COALESCE(json_extract(data_json,'$.Indicador'),''), COALESCE(json_extract(data_json,'$.DataReferencia'),''), COALESCE(json_extract(data_json,'$.baseCalculo'),''), COALESCE(json_extract(data_json,'$.Suavizada'),'')"
        vintage_identity = "COALESCE(json_extract(raw_value_json,'$.Indicador'),''), COALESCE(json_extract(raw_value_json,'$.DataReferencia'),''), COALESCE(json_extract(raw_value_json,'$.baseCalculo'),''), COALESCE(json_extract(raw_value_json,'$.Suavizada'),'')"
        focus_valid = "json_type(data_json,'$.Mediana') IN ('integer','real')"
        vintage_valid = "json_type(raw_value_json,'$.Mediana') IN ('integer','real')"
        utc_date = "date('now')"
        vintage_utc_date = utc_date
        snapshot_clock = "strftime('%Y-%m-%dT%H:%M:%fZ','now')"
        sidra_identity_json = "json_object('table_id', table_id, 'variable_id', variable_id, 'classification_id', classification_id, 'category_id', category_id, 'territory_level', territory_level, 'territory_code', territory_code, 'unit', unit, 'base_version', base_version)"
    statements = {
        "sgs": """SELECT series_code, date, value, fetched_at FROM (
            SELECT series_code, date, value, fetched_at,
                   ROW_NUMBER() OVER (PARTITION BY series_code ORDER BY date DESC, fetched_at DESC) AS rn
            FROM sgs
        ) latest WHERE rn = 1 ORDER BY series_code""",
        "tr_periods": """SELECT period_start, period_end, value, first_seen_at, payload_hash FROM (
            SELECT period_start, period_end, value, first_seen_at, payload_hash,
                   ROW_NUMBER() OVER (PARTITION BY period_start, period_end ORDER BY first_seen_at DESC) AS revision_rank
            FROM tr_period_observations
        ) latest WHERE revision_rank = 1 ORDER BY period_start DESC, period_end DESC LIMIT 500""",
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
            run_id, task_key, cycle_id, error_code, error_category, retry_not_before
            FROM source_extractions ORDER BY started_at DESC, extraction_id DESC LIMIT 300""",
        "source_acquisition_commits": """SELECT extraction_id, selected_route, candidate_id,
            observation_count, source_payload_sha256, committed_at
            FROM source_acquisition_commits ORDER BY committed_at DESC LIMIT 300""",
        "source_observation_origins": """SELECT extraction_id, source, contract_id, identity_json, reference_date,
            value_sha256, route, candidate_id, source_payload_sha256, source_published_at, captured_at,
            contract_version, profile_semantic_sha256, parser_semantic_sha256, proof_sha256
            FROM source_observation_origins ORDER BY captured_at DESC LIMIT 1000""",
        "candidate_origin_business": """SELECT 'bcb_sgs' AS source, CAST(series_code AS TEXT) AS series_code,
            date AS reference_date, value, NULL AS dataset_key, NULL AS identity_json
            FROM sgs ORDER BY date DESC LIMIT 3000""",
        "candidate_origin_tr_periods": """SELECT 'bcb_sgs' AS source, '226' AS series_code,
            period_start AS reference_date, period_end, value, NULL AS dataset_key, NULL AS identity_json
            FROM tr_period_observations ORDER BY period_start DESC LIMIT 1000""",
        "candidate_origin_sidra_business": f"""SELECT 'ibge_sidra' AS source, NULL AS series_code,
            reference_period AS reference_date, value, dataset_key,
            {sidra_identity_json} AS identity_json
            FROM sidra_observations ORDER BY reference_period DESC LIMIT 3000""",
        "source_canary_bindings": """SELECT operation_id, candidate_id, as_of, plan_sha256,
            proof_sha256, contract_version, profile_semantic_sha256, parser_semantic_sha256,
            actual_reference_period, captured_at, request_count, inserted_count, updated_count, bound_at
            FROM source_canary_bindings ORDER BY bound_at DESC LIMIT 300""",
        "source_acquisition_attempts": """SELECT extraction_id, source, indicator, candidate_id,
            route, status, started_at, finished_at, request_count, retry_count,
            error_code, error_category, reference_min, reference_max
            FROM source_acquisition_attempts ORDER BY started_at DESC LIMIT 1000""",
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
    *,
    origin_rows: list[tuple[object, ...]] | None = None,
    commit_receipt: tuple[object, ...] | None = None,
    canary_binding: CanaryBinding | None = None,
    freshness_meta: tuple[str, str] | None = None,
) -> None:
    validate_canary_receipt(canary_binding, commit_receipt)
    validate_canary_origin_rows(canary_binding, origin_rows)
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
    origin_sqlite = (
        "INSERT OR IGNORE INTO source_observation_origins "
        "(origin_id, extraction_id, source, contract_id, identity_json, reference_date, "
        "value_sha256, route, candidate_id, source_payload_sha256, source_published_at, captured_at, "
        "contract_version, profile_semantic_sha256, parser_semantic_sha256, proof_sha256) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)"
    )
    origin_postgres = (
        "INSERT INTO source_observation_origins "
        "(origin_id, extraction_id, source, contract_id, identity_json, reference_date, "
        "value_sha256, route, candidate_id, source_payload_sha256, source_published_at, captured_at, "
        "contract_version, profile_semantic_sha256, parser_semantic_sha256, proof_sha256) "
        "VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s) "
        "ON CONFLICT (extraction_id, source, contract_id, identity_json, value_sha256, route) DO NOTHING"
    )
    if _is_pg():
        conn = _get_pg_conn()
        try:
            with conn.cursor() as cur:
                from db.refresh_attempt import current_refresh_attempt
                active_attempt = current_refresh_attempt()
                if active_attempt is not None:
                    assert_current_refresh_attempt(conn, active_attempt)
                if canary_binding is not None:
                    inserted = _claim_canary_binding(cur, canary_binding, postgres=True)
                    if not inserted:
                        if commit_receipt is None or _claim_commit_receipt(cur, commit_receipt, postgres=True):
                            raise RuntimeError("canary binding exists without its completed receipt")
                        conn.commit()
                        return
                if commit_receipt is not None and not _claim_commit_receipt(
                    cur, commit_receipt, postgres=True
                ):
                    conn.commit()
                    return
                cur.executemany(vintage_postgres, vintage_rows)
                cur.executemany(current_postgres, current_rows)
                if origin_rows:
                    cur.executemany(origin_postgres, attested_origin_rows(origin_rows))
                if freshness_meta is not None:
                    cur.execute(
                        "INSERT INTO meta (key, value) VALUES (%s, %s) "
                        "ON CONFLICT (key) DO UPDATE SET value = EXCLUDED.value",
                        freshness_meta,
                    )
            conn.commit()
        except Exception:
            with suppress(Exception):
                conn.rollback()
            raise
        return
    with _sqlite_lock:
        conn = _get_conn()
        try:
            from db.refresh_attempt import current_refresh_attempt
            active_attempt = current_refresh_attempt()
            if active_attempt is not None:
                assert_current_refresh_attempt(conn, active_attempt)
            if canary_binding is not None:
                inserted = _claim_canary_binding(conn, canary_binding, postgres=False)
                if not inserted:
                    if commit_receipt is None or _claim_commit_receipt(conn, commit_receipt, postgres=False):
                        raise RuntimeError("canary binding exists without its completed receipt")
                    conn.commit()
                    return
            if commit_receipt is not None and not _claim_commit_receipt(
                conn, commit_receipt, postgres=False
            ):
                conn.commit()
                return
            conn.executemany(vintage_sqlite, vintage_rows)
            conn.executemany(current_sqlite, current_rows)
            if origin_rows:
                conn.executemany(origin_sqlite, attested_origin_rows(origin_rows))
            if freshness_meta is not None:
                conn.execute(
                    "INSERT OR REPLACE INTO meta (key, value) VALUES (?, ?)", freshness_meta
                )
            conn.commit()
        except Exception:
            with suppress(Exception):
                conn.rollback()
            raise


def attested_origin_rows(
    rows: Sequence[tuple[object, ...]],
) -> list[tuple[object, ...]]:
    """Keep legacy origin tuples valid while leaving their new attestation NULL."""
    normalized: list[tuple[object, ...]] = []
    for row in rows:
        if len(row) == 12:
            normalized.append((*row, None, None, None, None))
        elif len(row) == 16:
            normalized.append(row)
        else:
            raise ValueError("invalid source origin row shape")
    return normalized


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


def upsert_sgs(
    series_code: int,
    records: list[Record],
    *,
    first_seen_at: str | None = None,
    origin_rows: list[tuple[object, ...]] | None = None,
    commit_receipt: tuple[object, ...] | None = None,
    canary_binding: CanaryBinding | None = None,
    freshness_meta: tuple[str, str] | None = None,
):
    rows = []
    for r in records:
        try:
            iso_date = _parse_sgs_date(r["data"])
        except (KeyError, TypeError, ValueError):
            continue
        value = _finite_float(r.get("valor"))
        if value is not None:
            rows.append((series_code, iso_date, value))
    if not rows and not origin_rows and commit_receipt is None and canary_binding is None:
        return
    from indicator_catalog import INDICATOR_CATALOG

    series_key = next(
        (str(spec.key) for spec in INDICATOR_CATALOG.values() if int(spec.provider_code) == series_code),
        str(series_code),
    )
    vintage_rows = [
        _vintage_row(
            "bcb_sgs", series_key, iso_date, {"valor": value}, first_seen_at=first_seen_at
        )
        for _, iso_date, value in rows
    ]
    _persist_current_and_vintages(
        "INSERT INTO sgs (series_code, date, value) VALUES (?, ?, ?) "
        "ON CONFLICT (series_code, date) DO UPDATE SET value = excluded.value",
        "INSERT INTO sgs (series_code, date, value) VALUES (%s, %s, %s) "
        "ON CONFLICT (series_code, date) DO UPDATE SET value = EXCLUDED.value",
        rows,
        vintage_rows,
        origin_rows=origin_rows,
        commit_receipt=commit_receipt,
        canary_binding=canary_binding,
        freshness_meta=freshness_meta,
    )


def upsert_tr_periods(
    records: list[dict],
    *,
    first_seen_at: str | None = None,
    origin_rows: list[tuple[object, ...]] | None = None,
    commit_receipt: tuple[object, ...] | None = None,
) -> int:
    """Persist SGS 226 with its full published interval identity, append-only."""
    now = first_seen_at or _utc_now()
    rows: list[tuple[str, str, float, str, str]] = []
    vintage_rows: list[tuple] = []
    seen: set[tuple[str, str, str]] = set()
    capture_values: dict[tuple[str, str], float] = {}
    for record in records:
        try:
            start = _parse_sgs_date(record["data"])
            end = _parse_sgs_date(record["dataFim"])
        except (KeyError, TypeError, ValueError):
            continue
        value = _finite_float(record.get("valor"))
        if value is None or end <= start:
            continue
        payload = {"data": start, "dataFim": end, "valor": value}
        payload_hash = _payload_hash(payload)
        identity = (start, end, payload_hash)
        period_identity = (start, end)
        prior_value = capture_values.get(period_identity)
        if prior_value is not None and prior_value != value:
            raise ValueError("conflicting_tr_period_values_in_capture")
        capture_values[period_identity] = value
        if identity in seen:
            continue
        seen.add(identity)
        rows.append((start, end, value, now, payload_hash))
        vintage_rows.append(_vintage_row(
            "bcb_sgs", "tr", start, {"valor": value},
            dimensions={"dataFim": end}, first_seen_at=now,
        ))
    if not rows:
        return 0
    unique_rows: list[tuple[str, str, float, str, str]] = []
    unique_vintages: list[tuple] = []
    for row, vintage in zip(rows, vintage_rows, strict=True):
        start, end, value, seen_at, digest = row
        existing = _query(
            "SELECT value, first_seen_at, payload_hash FROM tr_period_observations "
            "WHERE period_start = ? AND period_end = ? ORDER BY first_seen_at DESC",
            (start, end),
        )
        if any(item["payload_hash"] == digest for item in existing):
            continue
        if existing and any(float(item["value"]) != value and str(item["first_seen_at"]) >= seen_at for item in existing):
            raise ValueError("ambiguous_tr_period_revision_capture_time")
        unique_rows.append(row)
        unique_vintages.append(vintage)
    if not unique_rows and not origin_rows and commit_receipt is None:
        return 0
    _persist_current_and_vintages(
        "INSERT OR IGNORE INTO tr_period_observations "
        "(period_start, period_end, value, first_seen_at, payload_hash) VALUES (?, ?, ?, ?, ?)",
        "INSERT INTO tr_period_observations "
        "(period_start, period_end, value, first_seen_at, payload_hash) VALUES (%s, %s, %s, %s, %s) "
        "ON CONFLICT (period_start, period_end, payload_hash) DO NOTHING",
        unique_rows,
        unique_vintages,
        origin_rows=origin_rows,
        commit_receipt=commit_receipt,
    )
    return len(unique_rows)


def query_tr_periods(
    start_date: str | None = None, end_date: str | None = None, known_at: str | None = None
) -> list[Record]:
    normalized_start = _normalize_sgs_date_bound(start_date) if start_date else None
    normalized_end = _normalize_sgs_date_bound(end_date) if end_date else None
    if normalized_start and normalized_end and normalized_start > normalized_end:
        raise ValueError("SGS start date must be on or before end date")
    sql = (
        "SELECT period_start, period_end, value FROM ("
        "SELECT period_start, period_end, value, first_seen_at, "
        "ROW_NUMBER() OVER (PARTITION BY period_start, period_end ORDER BY first_seen_at DESC) AS revision_rank "
        "FROM tr_period_observations WHERE 1=1) "
        "AS revisions WHERE revision_rank = 1"
    )
    params: list[object] = []
    if known_at:
        # Bound the revision selection before ranking, so callers can reproduce
        # the value known at an earlier capture instant.
        sql = (
            "SELECT period_start, period_end, value FROM ("
            "SELECT period_start, period_end, value, ROW_NUMBER() OVER "
            "(PARTITION BY period_start, period_end ORDER BY first_seen_at DESC) AS revision_rank "
            "FROM tr_period_observations WHERE first_seen_at <= ?) "
            "AS revisions WHERE revision_rank = 1"
        )
        params.append(known_at)
    if normalized_start:
        sql += " AND period_start >= ?"
        params.append(normalized_start)
    if normalized_end:
        sql += " AND period_start <= ?"
        params.append(normalized_end)
    sql += " ORDER BY period_start ASC, period_end ASC"
    return [
        {"data": _to_sgs_date(row["period_start"].isoformat() if isinstance(row["period_start"], date) else str(row["period_start"])), "dataFim": _to_sgs_date(row["period_end"].isoformat() if isinstance(row["period_end"], date) else str(row["period_end"])), "valor": str(row["value"])}
        for row in _query(sql, params)
    ]
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


def treasury_cohort_commit(extraction_id: str, *, reconnect: bool = False) -> TreasuryCohortReceipt | None:
    if reconnect:
        if _is_pg():
            conn = getattr(_pg_local, "conn", None)
            if conn is not None:
                with suppress(Exception):
                    conn.close()
                _pg_local.conn = None
        elif _conn is not None:
            with _sqlite_lock:
                _conn.close()
                globals()["_conn"] = None
    rows = _query(
        "SELECT extraction_id, source_payload_sha256, observation_count, reference_min, "
        "reference_max, captured_at FROM treasury_cohort_commits WHERE extraction_id = ?",
        (extraction_id,),
    )
    if not rows:
        return None
    row = rows[0]
    return TreasuryCohortReceipt(
        str(row["extraction_id"]), str(row["source_payload_sha256"]),
        int(row["observation_count"]), str(row["reference_min"]),
        str(row["reference_max"]), str(row["captured_at"]),
    )


def _treasury_time_remaining(
    deadline_at: float,
    clock: Callable[[], float],
    reserve_seconds: float = 0.0,
) -> float:
    remaining = deadline_at - clock() - reserve_seconds
    if remaining <= 0:
        raise TimeoutError("treasury cohort persistence deadline elapsed")
    return remaining


def _same_treasury_payload(left: TreasuryCohortReceipt, right: TreasuryCohortReceipt) -> bool:
    return left[:5] == right[:5]


def _assert_treasury_receipt_matches(
    existing: TreasuryCohortReceipt,
    proposed: TreasuryCohortReceipt,
) -> None:
    if not _same_treasury_payload(existing, proposed):
        raise ValueError("treasury extraction receipt conflicts with the supplied cohort")


def _postgres_treasury_receipt(row: Mapping[str, object], extraction_id: str) -> TreasuryCohortReceipt:
    return TreasuryCohortReceipt(
        extraction_id,
        str(row["source_payload_sha256"]),
        int(row["observation_count"]),
        str(row["reference_min"]),
        str(row["reference_max"]),
        str(row["captured_at"]),
    )


def _commit_treasury_cohort_postgres(
    rows: Sequence[tuple[int, str, str]],
    receipt: TreasuryCohortReceipt,
    deadline_at: float,
    clock: Callable[[], float],
    stage_timings: dict[str, float] | None,
) -> TreasuryCohortReceipt:
    remaining = _treasury_time_remaining(deadline_at, clock, 2.0)
    conn = _get_pg_conn()
    try:
        with conn.cursor() as cur:
            from db.refresh_attempt import current_refresh_attempt
            active_attempt = current_refresh_attempt()
            if active_attempt is not None:
                assert_current_refresh_attempt(conn, active_attempt)
            cur.execute("SELECT set_config('statement_timeout', %s, true)", (str(max(1, int(remaining * 1000))),))
            cur.execute("SELECT set_config('lock_timeout', %s, true)", (str(max(1, min(1000, int(remaining * 1000)))),))
            cur.execute(
                "SELECT source_payload_sha256, observation_count, reference_min, reference_max, captured_at "
                "FROM treasury_cohort_commits WHERE extraction_id = %s",
                (receipt.extraction_id,),
            )
            prior_row = cur.fetchone()
            if prior_row is not None:
                existing = _postgres_treasury_receipt(prior_row, receipt.extraction_id)
                _assert_treasury_receipt_matches(existing, receipt)
                conn.commit()
                return existing
            _treasury_time_remaining(deadline_at, clock, 2.0)
            stage_started = monotonic()
            cur.execute(
                "CREATE TEMP TABLE treasury_cohort_stage ("
                "bond_code INTEGER NOT NULL, base_date TEXT NOT NULL, data_json TEXT NOT NULL, "
                "PRIMARY KEY (bond_code, base_date)) ON COMMIT DROP"
            )
            with cur.copy("COPY treasury_cohort_stage (bond_code, base_date, data_json) FROM STDIN") as copy:
                for index, row in enumerate(rows):
                    if index % 1024 == 0:
                        _treasury_time_remaining(deadline_at, clock, 2.0)
                    copy.write_row(row)
            if stage_timings is not None:
                stage_timings["copy_stage_seconds"] = monotonic() - stage_started
            _treasury_time_remaining(deadline_at, clock, 2.0)
            replacement_started = monotonic()
            cur.execute("DELETE FROM treasury")
            cur.execute(
                "INSERT INTO treasury (bond_code, base_date, data_json) "
                "SELECT bond_code, base_date, data_json FROM treasury_cohort_stage"
            )
            if stage_timings is not None:
                stage_timings["atomic_replace_seconds"] = monotonic() - replacement_started
            _treasury_time_remaining(deadline_at, clock, 2.0)
            cur.execute(
                "INSERT INTO treasury_cohort_commits "
                "(extraction_id, source_payload_sha256, observation_count, reference_min, reference_max, captured_at) "
                "VALUES (%s, %s, %s, %s, %s, %s)",
                receipt,
            )
            cur.execute(
                "INSERT INTO meta (key, value) VALUES ('td_last_refresh', %s) "
                "ON CONFLICT (key) DO UPDATE SET value = EXCLUDED.value",
                (receipt.captured_at,),
            )
            _treasury_time_remaining(deadline_at, clock)
            if active_attempt is not None:
                assert_current_refresh_attempt(conn, active_attempt)
            if stage_timings is not None:
                stage_timings["receipt_and_freshness_seconds"] = monotonic() - replacement_started - (
                    stage_timings.get("atomic_replace_seconds", 0.0)
                )
        commit_started = monotonic()
        conn.commit()
        if stage_timings is not None:
            stage_timings["database_commit_seconds"] = monotonic() - commit_started
        return receipt
    except Exception:
        with suppress(Exception):
            conn.rollback()
        raise


def _commit_treasury_cohort_sqlite(
    rows: Sequence[tuple[int, str, str]],
    receipt: TreasuryCohortReceipt,
    deadline_at: float,
    clock: Callable[[], float],
) -> TreasuryCohortReceipt:
    remaining = _treasury_time_remaining(deadline_at, clock, 2.0)
    with _sqlite_lock:
        conn = _get_conn()
        old_timeout = int(conn.execute("PRAGMA busy_timeout").fetchone()[0])
        conn.execute(f"PRAGMA busy_timeout = {max(1, min(3000, int(remaining * 1000)))}")
        conn.set_progress_handler(lambda: int(clock() >= deadline_at), 1000)
        try:
            conn.execute("BEGIN IMMEDIATE")
            from db.refresh_attempt import current_refresh_attempt
            active_attempt = current_refresh_attempt()
            if active_attempt is not None:
                assert_current_refresh_attempt(conn, active_attempt)
            prior_row = conn.execute(
                "SELECT source_payload_sha256, observation_count, reference_min, reference_max, captured_at "
                "FROM treasury_cohort_commits WHERE extraction_id = ?",
                (receipt.extraction_id,),
            ).fetchone()
            if prior_row is not None:
                existing = TreasuryCohortReceipt(
                    receipt.extraction_id,
                    str(prior_row[0]),
                    int(prior_row[1]),
                    str(prior_row[2]),
                    str(prior_row[3]),
                    str(prior_row[4]),
                )
                _assert_treasury_receipt_matches(existing, receipt)
                conn.commit()
                return existing
            _treasury_time_remaining(deadline_at, clock, 2.0)
            conn.execute("DELETE FROM treasury")
            _treasury_time_remaining(deadline_at, clock, 2.0)
            conn.executemany(
                "INSERT INTO treasury (bond_code, base_date, data_json) VALUES (?, ?, ?)",
                rows,
            )
            _treasury_time_remaining(deadline_at, clock, 2.0)
            conn.execute(
                "INSERT INTO treasury_cohort_commits "
                "(extraction_id, source_payload_sha256, observation_count, reference_min, reference_max, captured_at) "
                "VALUES (?, ?, ?, ?, ?, ?)",
                receipt,
            )
            conn.execute("INSERT OR REPLACE INTO meta (key, value) VALUES ('td_last_refresh', ?)", (receipt.captured_at,))
            _treasury_time_remaining(deadline_at, clock)
            if active_attempt is not None:
                assert_current_refresh_attempt(conn, active_attempt)
            conn.commit()
            return receipt
        except Exception:
            with suppress(Exception):
                conn.rollback()
            raise
        finally:
            conn.set_progress_handler(None, 0)
            with suppress(sqlite3.Error):
                conn.execute(f"PRAGMA busy_timeout = {old_timeout}")


def commit_treasury_cohort(
    rows: Sequence[tuple[int, str, str]],
    receipt: TreasuryCohortReceipt,
    *,
    deadline_at: float,
    clock: Callable[[], float] = monotonic,
    stage_timings: dict[str, float] | None = None,
) -> TreasuryCohortReceipt:
    if not rows or len(rows) != receipt.observation_count:
        raise ValueError("treasury cohort receipt count does not match its non-empty rows")
    try:
        if _is_pg():
            return _commit_treasury_cohort_postgres(rows, receipt, deadline_at, clock, stage_timings)
        return _commit_treasury_cohort_sqlite(rows, receipt, deadline_at, clock)
    except Exception:
        # A lost COMMIT acknowledgement is resolved by the durable receipt, never by reloading blindly.
        committed = treasury_cohort_commit(receipt.extraction_id, reconnect=True)
        if committed is None:
            raise
        _assert_treasury_receipt_matches(committed, receipt)
        return committed


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
        execute_transaction(
            [(
                "INSERT INTO meta (key, value) VALUES (?, ?) ON CONFLICT (key) DO UPDATE SET value = EXCLUDED.value",
                [(key, value)],
            )]
        )
    else:
        execute_transaction(
            [("INSERT OR REPLACE INTO meta (key, value) VALUES (?, ?)", [(key, value)])]
        )


def get_meta(key: str) -> str | None:
    rows = _query("SELECT value FROM meta WHERE key = ?", (key,))
    return rows[0]["value"] if rows else None


def start_source_extraction(record: dict[str, object]) -> None:
    """Persist the start of a provider operation before network or parsing."""
    try:
        _query("SELECT attempt_id FROM source_extractions LIMIT 0")
    except Exception as error:
        message = str(error).lower()
        if not any(marker in message for marker in ("no such table", "does not exist", "no column", "undefined column")):
            raise
        init_db()
    fields = (
        "extraction_id", "source", "indicator", "qualifiers_json", "status",
        "network_called", "started_at", "run_id", "task_key", "cycle_id",
        "attempt_id", "claim_generation", "claim_expires_at",
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
        "retry_count", "error_code", "error_detail", "error_category", "retry_not_before",
    )
    assignments = ", ".join(f"{field} = ?" for field in fields)
    values = tuple(record.get(field) for field in fields) + (record["extraction_id"],)
    _execute(
        f"UPDATE source_extractions SET {assignments} "
        "WHERE extraction_id = ? AND status = 'running'",
        values,
    )


def reconcile_expired_refresh_attempts(run_id: str, now: str) -> int:
    """Record unknown outcomes only for newly linked attempts whose claim expired."""
    count = 0
    select = (
        "SELECT a.attempt_id FROM refresh_attempt_lifecycle a "
        "JOIN refresh_work_items i ON i.run_id = a.run_id AND i.item_key = a.item_key "
        "WHERE a.run_id = ? AND a.terminal_at IS NULL AND a.claim_expires_at <= ? "
        "AND i.status = 'claimed' AND i.attempt = a.claim_generation "
        "AND i.claim_token = a.claim_token AND i.claim_expires_at <= ?"
    )
    candidates = _query(select, (run_id, now, now))
    for candidate in candidates:
        attempt_id = str(candidate["attempt_id"])
        receipt = _query(
            "SELECT t.observation_count, t.reference_max AS source_data_date, t.extraction_id "
            "FROM treasury_cohort_commits t JOIN source_extractions e ON e.extraction_id = t.extraction_id "
            "WHERE e.attempt_id = ? UNION ALL "
            "SELECT c.observation_count, e.source_data_date, c.extraction_id "
            "FROM source_acquisition_commits c JOIN source_extractions e ON e.extraction_id = c.extraction_id "
            "WHERE e.attempt_id = ? LIMIT 1",
            (attempt_id, attempt_id),
        )
        has_receipt = bool(receipt)
        outcome = "committed" if has_receipt else "outcome_unknown"
        committed_count = int(receipt[0]["observation_count"]) if has_receipt else None
        source_date = receipt[0]["source_data_date"] if has_receipt else None
        evidence = f"durable_receipt:{receipt[0]['extraction_id']}" if has_receipt else "claim_expired_without_terminal_receipt"
        values = (attempt_id, now, outcome, committed_count, source_date, evidence)
        if _is_pg():
            conn = _get_pg_conn()
            with conn.cursor() as cur:
                cur.execute(
                    "INSERT INTO refresh_attempt_reconciliations "
                    "(attempt_id, observed_at, outcome, committed_count, source_data_date, evidence) "
                    "VALUES (%s, %s, %s, %s, %s, %s) ON CONFLICT (attempt_id, outcome) DO NOTHING "
                    "RETURNING reconciliation_id", values,
                )
                inserted = cur.fetchone() is not None
                if inserted:
                    cur.execute(
                        "UPDATE refresh_attempt_lifecycle SET terminal_outcome = %s, terminal_at = %s, "
                        "committed_count = %s, source_data_date = %s WHERE attempt_id = %s AND terminal_at IS NULL",
                        (outcome, now, committed_count, source_date, attempt_id),
                    )
                    cur.execute(
                        "INSERT INTO refresh_attempt_milestones "
                        "(attempt_id, stage, observed_at, outcome, detail_code) "
                        "VALUES (%s, 'reconciled', %s, %s, %s)",
                        (attempt_id, now, outcome, evidence),
                    )
            conn.commit()
        else:
            with _sqlite_lock:
                conn = _get_conn()
                cursor = conn.execute(
                    "INSERT INTO refresh_attempt_reconciliations "
                    "(attempt_id, observed_at, outcome, committed_count, source_data_date, evidence) "
                    "VALUES (?, ?, ?, ?, ?, ?) ON CONFLICT (attempt_id, outcome) DO NOTHING", values,
                )
                inserted = cursor.rowcount == 1
                if inserted:
                    conn.execute(
                        "UPDATE refresh_attempt_lifecycle SET terminal_outcome = ?, terminal_at = ?, "
                        "committed_count = ?, source_data_date = ? WHERE attempt_id = ? AND terminal_at IS NULL",
                        (outcome, now, committed_count, source_date, attempt_id),
                    )
                    conn.execute(
                        "INSERT INTO refresh_attempt_milestones "
                        "(attempt_id, stage, observed_at, outcome, detail_code) "
                        "VALUES (?, 'reconciled', ?, ?, ?)", (attempt_id, now, outcome, evidence),
                    )
                conn.commit()
        count += int(inserted)
    return count


def start_source_acquisition_attempt(record: dict[str, object]) -> None:
    fields = (
        "attempt_id", "extraction_id", "source", "indicator", "candidate_id",
        "route", "status", "started_at",
    )
    values = tuple(record.get(field) for field in fields)
    _execute(
        f"INSERT INTO source_acquisition_attempts ({', '.join(fields)}) "
        f"VALUES ({', '.join('?' for _ in fields)})",
        values,
    )


def finish_source_acquisition_attempt(record: dict[str, object]) -> None:
    fields = (
        "status", "finished_at", "request_count", "retry_count", "payload_sha256",
        "reference_min", "reference_max", "error_code", "error_category",
    )
    assignments = ", ".join(f"{field} = ?" for field in fields)
    values = tuple(record.get(field) for field in fields) + (record["attempt_id"],)
    _execute(
        f"UPDATE source_acquisition_attempts SET {assignments} "
        "WHERE attempt_id = ? AND status = 'running'",
        values,
    )


def source_acquisition_commit(
    extraction_id: str, *, reconnect: bool = False
) -> dict[str, object] | None:
    if reconnect:
        if _is_pg():
            conn = getattr(_pg_local, "conn", None)
            if conn is not None:
                with suppress(Exception):
                    conn.close()
                _pg_local.conn = None
        elif _conn is not None:
            with _sqlite_lock:
                _conn.close()
                globals()["_conn"] = None
    rows = _query(
        "SELECT extraction_id, selected_route, candidate_id, observation_count, "
        "source_payload_sha256, committed_at FROM source_acquisition_commits "
        "WHERE extraction_id = ?",
        (extraction_id,),
    )
    return rows[0] if rows else None


def source_canary_binding(operation_id: str) -> SourceCanaryBinding | None:
    """Read one immutable canary binding without exposing request credentials."""
    rows = _query(
        "SELECT operation_id, candidate_id, as_of, plan_sha256, proof_sha256, "
        "contract_version, profile_semantic_sha256, parser_semantic_sha256, actual_reference_period, "
        "captured_at, request_count, inserted_count, updated_count, bound_at "
        "FROM source_canary_bindings WHERE operation_id = ?",
        (operation_id,),
    )
    return cast(SourceCanaryBinding, rows[0]) if rows else None


@contextmanager
def canary_operation_lock(operation_id: str) -> Iterator[None]:
    """Serialize one idempotency key across workers while checking and executing it."""
    key = int.from_bytes(hashlib.sha256(operation_id.encode("utf-8")).digest()[:8], "big", signed=True)
    if _is_pg():
        import psycopg

        # This session must outlive all work performed under the lock. In
        # particular, receipt reconciliation may deliberately reconnect the
        # thread-local store connection after an uncertain commit.
        conn = psycopg.connect(DATABASE_URL, autocommit=True)
        acquired = False
        operation_error: BaseException | None = None
        try:
            with conn.cursor() as cursor:
                cursor.execute("SELECT pg_advisory_lock(%s)", (key,))
            acquired = True
            yield
        except BaseException as error:
            operation_error = error
            raise
        finally:
            cleanup_errors: list[Exception] = []
            if acquired:
                try:
                    with conn.cursor() as cursor:
                        cursor.execute("SELECT pg_advisory_unlock(%s)", (key,))
                        released = cursor.fetchone()
                    if released != (True,):
                        raise RuntimeError("PostgreSQL canary advisory lock was not held at release")
                except (psycopg.Error, RuntimeError) as error:
                    cleanup_errors.append(error)
            try:
                conn.close()
            except psycopg.Error as error:
                cleanup_errors.append(error)
            if cleanup_errors:
                if operation_error is None:
                    raise cleanup_errors[0]
                operation_error.add_note(
                    "Additionally failed to clean up PostgreSQL canary advisory lock: "
                    + "; ".join(repr(error) for error in cleanup_errors)
                )
        return
    database_key = hashlib.sha256(str(Path(DB_PATH).resolve()).encode("utf-8")).hexdigest()[:16]
    lock_path = Path(tempfile.gettempdir()) / f"xbray-{database_key}-{key & ((1 << 64) - 1):016x}.lock"
    descriptor = os.open(lock_path, os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW, 0o600)
    try:
        fcntl.flock(descriptor, fcntl.LOCK_EX)
        yield
    finally:
        fcntl.flock(descriptor, fcntl.LOCK_UN)
        os.close(descriptor)


def latest_source_extractions(limit: int = 300) -> list[dict[str, object]]:
    bounded_limit = max(1, min(limit, 1000))
    return _query(
        "SELECT * FROM source_extractions ORDER BY started_at DESC, extraction_id DESC LIMIT ?",
        (bounded_limit,),
    )


def source_extractions_for_refresh_run(run_id: str) -> list[dict[str, object]]:
    """Return bounded SGS window outcomes for one persisted refresh run."""
    if not run_id or len(run_id) > 128:
        raise ValueError("invalid refresh run id")
    return _query(
        "SELECT extraction_id, source, task_key, indicator, qualifiers_json, status, network_called, "
        "started_at, finished_at, "
        "received_count, valid_count, persisted_count, request_count "
        "FROM source_extractions WHERE run_id = ? AND "
        "(task_key LIKE ? OR task_key LIKE ?) "
        "ORDER BY started_at, extraction_id LIMIT 500",
        (run_id, "sgs_juros_w%", "sgs_inflacao_w%"),
    )


def source_access_states() -> dict[str, dict[str, object]]:
    """Read all bounded breaker scopes without attempting or leasing any probe."""
    rows = _query(
        "SELECT scope_id, consecutive_temporary_failures, state, opened_until, "
        "retry_not_before, probe_lease_until, updated_at FROM source_access_state "
        "ORDER BY scope_id LIMIT 256"
    )
    return {
        str(row["scope_id"]): row
        for row in rows
        if isinstance(row.get("scope_id"), str)
    }


def source_candidate_activity(limit: int = 200) -> dict[str, list[dict[str, object]]]:
    """Bounded operator/audit view of attempts, selected receipts, and origins."""
    bounded = max(1, min(limit, 500))
    extractions = _query(
        "SELECT e.extraction_id, e.source, e.indicator, e.status, e.started_at, "
        "e.finished_at, e.network_called, e.source_data_date, e.source_reference_min, "
        "e.source_reference_max, e.request_count, e.retry_count, e.error_code, "
        "e.error_category, e.retry_not_before, c.selected_route, c.candidate_id, "
        "c.observation_count, c.source_payload_sha256, c.committed_at "
        "FROM source_extractions e LEFT JOIN source_acquisition_commits c "
        "ON c.extraction_id = e.extraction_id "
        "ORDER BY e.started_at DESC, e.extraction_id DESC LIMIT ?", (bounded,),
    )
    attempts = _query(
        "SELECT extraction_id, source, indicator, candidate_id, route, status, "
        "started_at, finished_at, request_count, retry_count, error_code, "
        "error_category, reference_min, reference_max "
        "FROM source_acquisition_attempts ORDER BY started_at DESC LIMIT ?", (bounded * 2,),
    )
    origins = _query(
        "SELECT extraction_id, source, contract_id, identity_json, reference_date, value_sha256, "
        "route, candidate_id, source_payload_sha256, source_published_at, captured_at, "
        "contract_version, profile_semantic_sha256, parser_semantic_sha256, proof_sha256 "
        "FROM source_observation_origins ORDER BY captured_at DESC LIMIT ?", (bounded * 10,),
    )
    bindings = _query(
        "SELECT operation_id, candidate_id, as_of, plan_sha256, proof_sha256, contract_version, "
        "profile_semantic_sha256, parser_semantic_sha256, actual_reference_period, captured_at, "
        "request_count, inserted_count, updated_count, bound_at "
        "FROM source_canary_bindings ORDER BY bound_at DESC LIMIT ?", (bounded,),
    )
    return {"extractions": extractions, "attempts": attempts, "origins": origins, "bindings": bindings}


def claim_source_access_probe(
    scope_id: str, now: str, lease_until: str
) -> tuple[bool, str | None, bool]:
    """Atomically admit a request or claim the sole expired-circuit probe."""
    sql = """INSERT INTO source_access_state (scope_id, state, updated_at)
        VALUES (?, 'closed', ?)
        ON CONFLICT(scope_id) DO UPDATE SET
            state = CASE WHEN source_access_state.state IN ('open', 'half_open') THEN 'half_open'
                         ELSE source_access_state.state END,
            probe_lease_until = CASE WHEN source_access_state.state IN ('open', 'half_open')
                                     THEN ? ELSE source_access_state.probe_lease_until END,
            updated_at = excluded.updated_at
        WHERE source_access_state.state = 'closed'
           OR (source_access_state.state = 'open'
               AND (source_access_state.opened_until IS NULL OR source_access_state.opened_until <= ?)
               AND (source_access_state.retry_not_before IS NULL OR source_access_state.retry_not_before <= ?)
               AND (source_access_state.probe_lease_until IS NULL
                    OR source_access_state.probe_lease_until <= ?))
           OR (source_access_state.state = 'half_open'
               AND (source_access_state.probe_lease_until IS NULL
                    OR source_access_state.probe_lease_until <= ?))
        RETURNING state, retry_not_before"""
    params = (scope_id, now, lease_until, now, now, now, now)
    row: dict[str, object] | None
    if _is_pg():
        conn = _get_pg_conn()
        with conn.cursor() as cur:
            cur.execute(sql.replace("?", "%s"), params)
            row = cast(dict[str, object] | None, cur.fetchone())
        conn.commit()
    else:
        with _sqlite_lock:
            conn = _get_conn()
            sqlite_row = conn.execute(sql, params).fetchone()
            row = dict(sqlite_row) if sqlite_row is not None else None
            conn.commit()
    if row is not None:
        return (
            True,
            _optional_timestamp_text(row.get("retry_not_before")),
            row.get("state") == "half_open",
        )
    current = cast(
        list[dict[str, object]],
        _query("SELECT retry_not_before FROM source_access_state WHERE scope_id = ?", (scope_id,)),
    )
    retry_value = current[0].get("retry_not_before") if current else None
    return False, _optional_timestamp_text(retry_value), False


def _optional_timestamp_text(value: object) -> str | None:
    if isinstance(value, datetime):
        return value.astimezone(timezone.utc).isoformat()
    return value if isinstance(value, str) else None


def record_source_access_failure(
    scope_id: str, now: str, opened_until: str, retry_not_before: str | None, *,
    rate_limited: bool, failure_count: int = 1,
) -> None:
    """Record only classified transport failures or an explicit provider 429."""
    failures = 0 if rate_limited else max(1, min(failure_count, 3))
    has_provider_cooldown = retry_not_before is not None
    state = "open" if rate_limited or has_provider_cooldown or failures >= 3 else "closed"
    initial_opened_until = opened_until if state == "open" else None
    timestamp_cast = "::timestamptz" if _is_pg() else ""
    sql = f"""INSERT INTO source_access_state
        (scope_id, consecutive_temporary_failures, state, opened_until,
         retry_not_before, probe_lease_until, updated_at)
        VALUES (?, ?, ?, ?, ?, NULL, ?)
        ON CONFLICT(scope_id) DO UPDATE SET
            consecutive_temporary_failures = CASE WHEN ? THEN source_access_state.consecutive_temporary_failures
                                                  ELSE source_access_state.consecutive_temporary_failures + ? END,
            state = CASE WHEN ? OR ? OR (NOT ? AND source_access_state.consecutive_temporary_failures + ? >= 3)
                         THEN 'open' ELSE 'closed' END,
            opened_until = CASE WHEN ? OR ? OR (NOT ? AND source_access_state.consecutive_temporary_failures + ? >= 3)
                                 THEN ?{timestamp_cast} ELSE NULL END,
            retry_not_before = CASE WHEN ? THEN ?
                                    ELSE source_access_state.retry_not_before END,
            probe_lease_until = NULL, updated_at = excluded.updated_at"""
    params = (
        scope_id, failures, state, initial_opened_until, retry_not_before, now,
        rate_limited, failures,
        rate_limited, has_provider_cooldown, rate_limited, failures,
        rate_limited, has_provider_cooldown, rate_limited, failures, opened_until,
        retry_not_before is not None, retry_not_before,
    )
    _execute(sql, params)


def record_source_access_success(scope_id: str, now: str) -> None:
    _execute(
        """INSERT INTO source_access_state
           (scope_id, consecutive_temporary_failures, state, updated_at)
           VALUES (?, 0, 'closed', ?)
           ON CONFLICT(scope_id) DO UPDATE SET consecutive_temporary_failures = 0,
             state = 'closed', opened_until = NULL, retry_not_before = NULL,
             probe_lease_until = NULL, updated_at = excluded.updated_at""",
        (scope_id, now),
    )


def release_source_access_probe(scope_id: str, now: str, opened_until: str) -> None:
    """Release a half-open claim after a non-transport validation/config failure."""
    _execute(
        """UPDATE source_access_state SET state = 'open', opened_until = ?,
             probe_lease_until = NULL, updated_at = ?
           WHERE scope_id = ? AND state = 'half_open'""",
        (opened_until, now, scope_id),
    )


def get_source_access_state(scope_id: str) -> dict[str, object] | None:
    rows = _query(
        "SELECT scope_id, state, retry_not_before FROM source_access_state WHERE scope_id = ?",
        (scope_id,),
    )
    return rows[0] if rows else None


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
    origin_rows: list[tuple[object, ...]] | None = None,
    commit_receipt: tuple[object, ...] | None = None,
    canary_binding: CanaryBinding | None = None,
) -> None:
    from db.sidra_store import insert_sidra_observations as insert

    insert(
        observations,
        first_seen_at=first_seen_at,
        origin_rows=origin_rows,
        commit_receipt=commit_receipt,
        canary_binding=canary_binding,
    )


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
    from db.refresh_attempt import current_refresh_attempt
    active_attempt = current_refresh_attempt()
    if active_attempt is not None:
        if _is_pg():
            conn = _get_pg_conn()
            try:
                with conn.cursor() as cursor:
                    assert_current_refresh_attempt(conn, active_attempt)
                    cursor.execute(
                        "UPDATE refresh_task_status SET finished_at = %s, last_success = %s, status = %s, "
                        "series_updated = %s, record_count = %s, duration_ms = %s, error = %s WHERE task = %s",
                        (finished_at, preserved_success, status, series_updated, record_count,
                         duration_ms, error_code, task),
                    )
                conn.commit()
            except Exception:
                with suppress(Exception):
                    conn.rollback()
                raise
        else:
            with _sqlite_lock:
                conn = _get_conn()
                try:
                    conn.execute("BEGIN IMMEDIATE")
                    assert_current_refresh_attempt(conn, active_attempt)
                    conn.execute(
                        "UPDATE refresh_task_status SET finished_at = ?, last_success = ?, status = ?, "
                        "series_updated = ?, record_count = ?, duration_ms = ?, error = ? WHERE task = ?",
                        (finished_at, preserved_success, status, series_updated, record_count,
                         duration_ms, error_code, task),
                    )
                    conn.commit()
                except Exception:
                    with suppress(Exception):
                        conn.rollback()
                    raise
        return
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
