from typing import Final

MIGRATION_NAME: Final = "018_refresh_attempt_lifecycle"

_ATTEMPTS = """CREATE TABLE IF NOT EXISTS refresh_attempt_lifecycle (
    attempt_id TEXT PRIMARY KEY,
    run_id TEXT NOT NULL,
    item_key TEXT NOT NULL,
    attempt_number INTEGER NOT NULL CHECK (attempt_number > 0),
    claim_generation INTEGER NOT NULL CHECK (claim_generation > 0),
    claim_token TEXT NOT NULL,
    claim_expires_at TEXT NOT NULL,
    started_at TEXT NOT NULL,
    last_stage TEXT NOT NULL,
    terminal_outcome TEXT,
    terminal_at TEXT,
    last_error_code TEXT,
    committed_count INTEGER,
    source_data_date TEXT,
    UNIQUE (run_id, item_key, attempt_number)
)"""

_MILESTONES_SQLITE = """CREATE TABLE IF NOT EXISTS refresh_attempt_milestones (
    milestone_id INTEGER PRIMARY KEY AUTOINCREMENT,
    attempt_id TEXT NOT NULL REFERENCES refresh_attempt_lifecycle(attempt_id),
    stage TEXT NOT NULL,
    observed_at TEXT NOT NULL,
    outcome TEXT NOT NULL,
    detail_code TEXT
)"""

_RECONCILIATIONS_SQLITE = """CREATE TABLE IF NOT EXISTS refresh_attempt_reconciliations (
    reconciliation_id INTEGER PRIMARY KEY AUTOINCREMENT,
    attempt_id TEXT NOT NULL REFERENCES refresh_attempt_lifecycle(attempt_id),
    observed_at TEXT NOT NULL,
    outcome TEXT NOT NULL CHECK (outcome IN ('outcome_unknown', 'committed')),
    committed_count INTEGER,
    source_data_date TEXT,
    evidence TEXT NOT NULL,
    UNIQUE (attempt_id, outcome)
)"""

_INDEX = "CREATE INDEX IF NOT EXISTS refresh_attempt_lifecycle_item_idx ON refresh_attempt_lifecycle (run_id, item_key, claim_expires_at)"
_ALTER_EXTRACTIONS = (
    "ALTER TABLE source_extractions ADD COLUMN attempt_id TEXT",
    "ALTER TABLE source_extractions ADD COLUMN claim_generation INTEGER",
    "ALTER TABLE source_extractions ADD COLUMN claim_expires_at TEXT",
)
_MILESTONES_POSTGRES = _MILESTONES_SQLITE.replace("INTEGER PRIMARY KEY AUTOINCREMENT", "BIGSERIAL PRIMARY KEY")
_RECONCILIATIONS_POSTGRES = _RECONCILIATIONS_SQLITE.replace("INTEGER PRIMARY KEY AUTOINCREMENT", "BIGSERIAL PRIMARY KEY")
SQLITE_STATEMENTS: Final[tuple[str, ...]] = (_ATTEMPTS, _MILESTONES_SQLITE, _RECONCILIATIONS_SQLITE, _INDEX, *_ALTER_EXTRACTIONS)
POSTGRES_STATEMENTS: Final[tuple[str, ...]] = (_ATTEMPTS, _MILESTONES_POSTGRES, _RECONCILIATIONS_POSTGRES, _INDEX,
    "ALTER TABLE source_extractions ADD COLUMN IF NOT EXISTS attempt_id TEXT",
    "ALTER TABLE source_extractions ADD COLUMN IF NOT EXISTS claim_generation INTEGER",
    "ALTER TABLE source_extractions ADD COLUMN IF NOT EXISTS claim_expires_at TEXT")
