from typing import Final

MIGRATION_NAME: Final = "017_treasury_cohort_receipts"

_RECEIPTS = """CREATE TABLE IF NOT EXISTS treasury_cohort_commits (
    extraction_id TEXT PRIMARY KEY,
    source_payload_sha256 TEXT NOT NULL,
    observation_count INTEGER NOT NULL CHECK (observation_count > 0),
    reference_min TEXT NOT NULL,
    reference_max TEXT NOT NULL,
    captured_at TEXT NOT NULL
)"""

SQLITE_STATEMENTS: Final[tuple[str, ...]] = (_RECEIPTS,)
POSTGRES_STATEMENTS: Final[tuple[str, ...]] = SQLITE_STATEMENTS
