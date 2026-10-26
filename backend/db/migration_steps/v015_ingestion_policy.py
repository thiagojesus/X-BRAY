from typing import Final

MIGRATION_NAME: Final = "015_ingestion_policy"

SQLITE_STATEMENTS: Final[tuple[str, ...]] = (
    "ALTER TABLE source_extractions ADD COLUMN error_category TEXT",
    "ALTER TABLE source_extractions ADD COLUMN retry_not_before TEXT",
    """CREATE TABLE source_access_state (
        scope_id TEXT PRIMARY KEY,
        consecutive_temporary_failures INTEGER NOT NULL DEFAULT 0 CHECK (consecutive_temporary_failures >= 0),
        state TEXT NOT NULL DEFAULT 'closed' CHECK (state IN ('closed', 'open', 'half_open')),
        opened_until TEXT,
        retry_not_before TEXT,
        probe_lease_until TEXT,
        updated_at TEXT NOT NULL
    )""",
)

POSTGRES_STATEMENTS: Final[tuple[str, ...]] = (
    "ALTER TABLE source_extractions ADD COLUMN error_category TEXT",
    "ALTER TABLE source_extractions ADD COLUMN retry_not_before TIMESTAMPTZ",
    """CREATE TABLE source_access_state (
        scope_id TEXT PRIMARY KEY,
        consecutive_temporary_failures INTEGER NOT NULL DEFAULT 0 CHECK (consecutive_temporary_failures >= 0),
        state TEXT NOT NULL DEFAULT 'closed' CHECK (state IN ('closed', 'open', 'half_open')),
        opened_until TIMESTAMPTZ,
        retry_not_before TIMESTAMPTZ,
        probe_lease_until TIMESTAMPTZ,
        updated_at TIMESTAMPTZ NOT NULL
    )""",
)
