from typing import Final

MIGRATION_NAME: Final = "001_observability"

SQLITE_STATEMENTS: Final[tuple[str, ...]] = (
    """CREATE TABLE IF NOT EXISTS refresh_task_status (
        task TEXT PRIMARY KEY,
        provider TEXT NOT NULL,
        started_at TEXT,
        finished_at TEXT,
        last_attempt TEXT,
        last_success TEXT,
        status TEXT NOT NULL,
        series_expected INTEGER NOT NULL DEFAULT 0,
        series_updated INTEGER NOT NULL DEFAULT 0,
        record_count INTEGER NOT NULL DEFAULT 0,
        duration_ms INTEGER NOT NULL DEFAULT 0,
        error TEXT
    )""",
    """CREATE TABLE IF NOT EXISTS refresh_leases (
        lock_key TEXT PRIMARY KEY,
        owner TEXT NOT NULL,
        expires_at TEXT NOT NULL
    )""",
    """CREATE TABLE IF NOT EXISTS observation_vintages (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        provider TEXT NOT NULL,
        series_key TEXT NOT NULL,
        reference_period TEXT NOT NULL,
        dimensions_json TEXT NOT NULL,
        dimensions_hash TEXT NOT NULL,
        raw_value_json TEXT NOT NULL,
        source_published_at TEXT,
        first_seen_at TEXT NOT NULL,
        available_at TEXT NOT NULL,
        payload_hash TEXT NOT NULL,
        revision_id TEXT NOT NULL,
        UNIQUE (provider, series_key, reference_period, dimensions_hash, payload_hash)
    )""",
    "CREATE INDEX IF NOT EXISTS observation_vintages_current_idx ON observation_vintages (provider, series_key, reference_period, available_at, first_seen_at, payload_hash)",
)

POSTGRES_STATEMENTS: Final[tuple[str, ...]] = (
    SQLITE_STATEMENTS[0],
    SQLITE_STATEMENTS[1],
    SQLITE_STATEMENTS[2].replace("id INTEGER PRIMARY KEY AUTOINCREMENT", "id BIGSERIAL PRIMARY KEY"),
    SQLITE_STATEMENTS[3],
)
