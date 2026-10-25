from typing import Final

MIGRATION_NAME: Final = "013_source_extractions"

_TABLE = """CREATE TABLE IF NOT EXISTS source_extractions (
    extraction_id TEXT PRIMARY KEY,
    source TEXT NOT NULL,
    indicator TEXT NOT NULL,
    qualifiers_json TEXT NOT NULL DEFAULT '{}',
    status TEXT NOT NULL CHECK (status IN (
        'running', 'success', 'partial', 'cache_hit', 'empty',
        'network_error', 'validation_error', 'persistence_error', 'freshness_error', 'logging_error'
    )),
    network_called INTEGER NOT NULL CHECK (network_called IN (0, 1)),
    started_at TEXT NOT NULL,
    finished_at TEXT,
    extracted_at TEXT,
    source_data_date TEXT,
    source_reference_min TEXT,
    source_reference_max TEXT,
    received_count INTEGER,
    valid_count INTEGER,
    persisted_count INTEGER,
    inserted_count INTEGER,
    updated_count INTEGER,
    request_count INTEGER,
    retry_count INTEGER,
    run_id TEXT,
    task_key TEXT,
    cycle_id TEXT,
    error_code TEXT,
    error_detail TEXT,
    CHECK (received_count IS NULL OR received_count >= 0),
    CHECK (valid_count IS NULL OR valid_count >= 0),
    CHECK (persisted_count IS NULL OR persisted_count >= 0),
    CHECK (inserted_count IS NULL OR inserted_count >= 0),
    CHECK (updated_count IS NULL OR updated_count >= 0)
)"""

SQLITE_STATEMENTS: Final[tuple[str, ...]] = (
    _TABLE,
    """CREATE TABLE IF NOT EXISTS focus_forecasts (
        indicator TEXT NOT NULL,
        date TEXT NOT NULL,
        identity_key TEXT NOT NULL,
        data_json TEXT NOT NULL,
        fetched_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ','now')),
        PRIMARY KEY (indicator, date, identity_key)
    )""",
    """INSERT OR IGNORE INTO focus_forecasts (indicator, date, identity_key, data_json, fetched_at)
        SELECT indicator, date,
            COALESCE(json_extract(data_json, '$.DataReferencia'), '') || '|' ||
            COALESCE(json_extract(data_json, '$.baseCalculo'), '') || '|' ||
            COALESCE(json_extract(data_json, '$.Suavizada'), ''), data_json, fetched_at
        FROM focus""",
    "CREATE INDEX IF NOT EXISTS focus_forecasts_latest_idx ON focus_forecasts (indicator, date DESC, fetched_at DESC)",
    "CREATE INDEX IF NOT EXISTS source_extractions_indicator_idx ON source_extractions (source, indicator, finished_at)",
    "CREATE INDEX IF NOT EXISTS source_extractions_run_idx ON source_extractions (run_id, task_key, started_at)",
)
POSTGRES_STATEMENTS: Final[tuple[str, ...]] = (
    _TABLE,
    """CREATE TABLE IF NOT EXISTS focus_forecasts (
        indicator TEXT NOT NULL,
        date DATE NOT NULL,
        identity_key TEXT NOT NULL,
        data_json TEXT NOT NULL,
        fetched_at TEXT NOT NULL DEFAULT (to_char(CURRENT_TIMESTAMP AT TIME ZONE 'UTC','YYYY-MM-DD\"T\"HH24:MI:SS.US\"Z\"')),
        PRIMARY KEY (indicator, date, identity_key)
    )""",
    """DO $migration$
        BEGIN
            IF to_regclass('focus') IS NOT NULL THEN
                INSERT INTO focus_forecasts (indicator, date, identity_key, data_json, fetched_at)
                SELECT indicator, date::date,
                    COALESCE(data_json::jsonb ->> 'DataReferencia', '') || '|' ||
                    COALESCE(data_json::jsonb ->> 'baseCalculo', '') || '|' ||
                    COALESCE(data_json::jsonb ->> 'Suavizada', ''), data_json, fetched_at
                FROM focus ON CONFLICT (indicator, date, identity_key) DO NOTHING;
            END IF;
        END $migration$""",
    "CREATE INDEX IF NOT EXISTS focus_forecasts_latest_idx ON focus_forecasts (indicator, date DESC, fetched_at DESC)",
    "CREATE INDEX IF NOT EXISTS source_extractions_indicator_idx ON source_extractions (source, indicator, finished_at)",
    "CREATE INDEX IF NOT EXISTS source_extractions_run_idx ON source_extractions (run_id, task_key, started_at)",
)
