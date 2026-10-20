from typing import Final

MIGRATION_NAME: Final = "007_retrospective_di"

SQLITE_STATEMENTS: Final[tuple[str, ...]] = (
    """CREATE TABLE IF NOT EXISTS retrospective_di_observations (
        trade_date TEXT NOT NULL,
        symbol TEXT NOT NULL,
        maturity TEXT NOT NULL,
        rate REAL NOT NULL,
        source_hash TEXT NOT NULL,
        fetched_at TEXT NOT NULL,
        revision TEXT NOT NULL,
        source_basis TEXT NOT NULL,
        source_name TEXT NOT NULL,
        source_member TEXT NOT NULL,
        PRIMARY KEY (trade_date, symbol, source_hash, source_member)
    )""",
    "CREATE INDEX IF NOT EXISTS retrospective_di_date_idx ON retrospective_di_observations (trade_date, symbol)",
)

POSTGRES_STATEMENTS: Final[tuple[str, ...]] = SQLITE_STATEMENTS
