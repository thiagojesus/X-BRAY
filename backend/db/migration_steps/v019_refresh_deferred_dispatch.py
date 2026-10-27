from typing import Final

MIGRATION_NAME: Final = "019_refresh_deferred_dispatch"

SQLITE_STATEMENTS: Final[tuple[str, ...]] = (
    "ALTER TABLE refresh_work_items ADD COLUMN claim_generation INTEGER NOT NULL DEFAULT 0",
    "ALTER TABLE refresh_work_items ADD COLUMN acquisition_attempts INTEGER NOT NULL DEFAULT 0",
    "ALTER TABLE refresh_work_items ADD COLUMN next_attempt_at TEXT",
    "ALTER TABLE refresh_work_items ADD COLUMN deferral_reason TEXT",
    "ALTER TABLE refresh_work_items ADD COLUMN last_error_category TEXT",
    "ALTER TABLE refresh_work_items ADD COLUMN last_error_code TEXT",
    "UPDATE refresh_work_items SET claim_generation = attempt WHERE claim_generation = 0",
    "ALTER TABLE refresh_attempt_lifecycle ADD COLUMN outbound_started_at TEXT",
)
POSTGRES_STATEMENTS: Final[tuple[str, ...]] = (
    "ALTER TABLE refresh_work_items ADD COLUMN IF NOT EXISTS claim_generation INTEGER NOT NULL DEFAULT 0",
    "ALTER TABLE refresh_work_items ADD COLUMN IF NOT EXISTS acquisition_attempts INTEGER NOT NULL DEFAULT 0",
    "ALTER TABLE refresh_work_items ADD COLUMN IF NOT EXISTS next_attempt_at TEXT",
    "ALTER TABLE refresh_work_items ADD COLUMN IF NOT EXISTS deferral_reason TEXT",
    "ALTER TABLE refresh_work_items ADD COLUMN IF NOT EXISTS last_error_category TEXT",
    "ALTER TABLE refresh_work_items ADD COLUMN IF NOT EXISTS last_error_code TEXT",
    "UPDATE refresh_work_items SET claim_generation = attempt WHERE claim_generation = 0",
    "ALTER TABLE refresh_attempt_lifecycle ADD COLUMN IF NOT EXISTS outbound_started_at TEXT",
)
