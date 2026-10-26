from typing import Final

MIGRATION_NAME: Final = "014_tr_period_observations"

SQLITE_STATEMENTS: Final[tuple[str, ...]] = (
    """CREATE TABLE IF NOT EXISTS tr_period_observations (
        period_start TEXT NOT NULL,
        period_end TEXT NOT NULL,
        value REAL NOT NULL,
        first_seen_at TEXT NOT NULL,
        payload_hash TEXT NOT NULL,
        PRIMARY KEY (period_start, period_end, payload_hash),
        CHECK (period_end > period_start)
    )""",
    "CREATE INDEX IF NOT EXISTS tr_period_latest_idx ON tr_period_observations (period_start DESC, period_end DESC)",
)

POSTGRES_STATEMENTS: Final[tuple[str, ...]] = (
    """CREATE TABLE IF NOT EXISTS tr_period_observations (
        period_start DATE NOT NULL,
        period_end DATE NOT NULL,
        value DOUBLE PRECISION NOT NULL,
        first_seen_at TIMESTAMPTZ NOT NULL,
        payload_hash TEXT NOT NULL,
        PRIMARY KEY (period_start, period_end, payload_hash),
        CHECK (period_end > period_start)
    )""",
    "CREATE INDEX IF NOT EXISTS tr_period_latest_idx ON tr_period_observations (period_start DESC, period_end DESC)",
)
