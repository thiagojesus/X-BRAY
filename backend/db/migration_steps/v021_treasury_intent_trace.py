from typing import Final

MIGRATION_NAME: Final = "021_treasury_intent_trace"

SQLITE_STATEMENTS: Final = (
    "ALTER TABLE treasury_cohort_commits ADD COLUMN run_id TEXT",
    "ALTER TABLE treasury_cohort_commits ADD COLUMN task_key TEXT",
    "ALTER TABLE treasury_cohort_commits ADD COLUMN contract_version TEXT",
    "ALTER TABLE treasury_cohort_commits ADD COLUMN dataset_uuid TEXT",
    "ALTER TABLE treasury_cohort_commits ADD COLUMN cohort_scope_sha256 TEXT",
    "ALTER TABLE treasury_cohort_commits ADD COLUMN cohort_rows_sha256 TEXT",
    "ALTER TABLE source_extractions ADD COLUMN request_trace_json TEXT",
    "ALTER TABLE source_access_state ADD COLUMN admission_generation INTEGER NOT NULL DEFAULT 0",
    (
        "CREATE INDEX IF NOT EXISTS treasury_cohort_intent_idx ON treasury_cohort_commits "
        "(run_id, task_key, contract_version, dataset_uuid, cohort_scope_sha256)"
    ),
)

POSTGRES_STATEMENTS: Final = (
    "ALTER TABLE treasury_cohort_commits ADD COLUMN IF NOT EXISTS run_id TEXT",
    "ALTER TABLE treasury_cohort_commits ADD COLUMN IF NOT EXISTS task_key TEXT",
    "ALTER TABLE treasury_cohort_commits ADD COLUMN IF NOT EXISTS contract_version TEXT",
    "ALTER TABLE treasury_cohort_commits ADD COLUMN IF NOT EXISTS dataset_uuid TEXT",
    "ALTER TABLE treasury_cohort_commits ADD COLUMN IF NOT EXISTS cohort_scope_sha256 TEXT",
    "ALTER TABLE treasury_cohort_commits ADD COLUMN IF NOT EXISTS cohort_rows_sha256 TEXT",
    "ALTER TABLE source_extractions ADD COLUMN IF NOT EXISTS request_trace_json TEXT",
    "ALTER TABLE source_access_state ADD COLUMN IF NOT EXISTS admission_generation BIGINT NOT NULL DEFAULT 0",
    (
        "CREATE INDEX IF NOT EXISTS treasury_cohort_intent_idx ON treasury_cohort_commits "
        "(run_id, task_key, contract_version, dataset_uuid, cohort_scope_sha256)"
    ),
)
