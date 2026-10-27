from typing import Final

MIGRATION_NAME: Final = "020_sgs_incremental_coverage"

_SQLITE = (
    ("CREATE TABLE IF NOT EXISTS sgs_refresh_plans ("
    "run_id TEXT PRIMARY KEY, plan_version TEXT NOT NULL, as_of TEXT NOT NULL, "
    "plan_json TEXT NOT NULL, plan_sha256 TEXT NOT NULL, created_at TEXT NOT NULL)"),
    ("CREATE TABLE IF NOT EXISTS sgs_refresh_plan_units ("
    "run_id TEXT NOT NULL, unit_id TEXT NOT NULL, ordinal INTEGER NOT NULL, role TEXT NOT NULL, "
        "series_code INTEGER NOT NULL, indicator_key TEXT NOT NULL, group_name TEXT NOT NULL, "
        "freshness_days INTEGER NOT NULL, policy_version TEXT NOT NULL, "
    "contract_version TEXT NOT NULL, identity_kind TEXT NOT NULL, unit TEXT NOT NULL, method TEXT NOT NULL, "
    "frequency TEXT NOT NULL, start_date TEXT NOT NULL, end_date TEXT NOT NULL, "
    "status TEXT NOT NULL DEFAULT 'reserved', last_error_code TEXT, updated_at TEXT NOT NULL, "
    "PRIMARY KEY (run_id, unit_id), FOREIGN KEY (run_id) REFERENCES sgs_refresh_plans(run_id))"),
    ("CREATE INDEX IF NOT EXISTS sgs_refresh_plan_units_status_idx "
    "ON sgs_refresh_plan_units (status, role, series_code, start_date)"),
    ("CREATE TABLE IF NOT EXISTS sgs_archive_cursors ("
    "source TEXT PRIMARY KEY, last_series_code INTEGER, updated_at TEXT NOT NULL)"),
    ("CREATE TABLE IF NOT EXISTS sgs_coverage_receipts ("
    "operation_id TEXT PRIMARY KEY, run_id TEXT NOT NULL, unit_id TEXT NOT NULL, source TEXT NOT NULL, "
    "series_code INTEGER NOT NULL, contract_id TEXT NOT NULL, contract_version TEXT NOT NULL, "
    "identity_kind TEXT NOT NULL, scope_role TEXT NOT NULL, policy_version TEXT NOT NULL, "
    "requested_start TEXT NOT NULL, requested_end TEXT NOT NULL, payload_sha256 TEXT NOT NULL, "
        "observed_count INTEGER NOT NULL CHECK (observed_count > 0), captured_at TEXT NOT NULL, "
        "reference_start TEXT NOT NULL, reference_end TEXT NOT NULL, "
    "business_receipt_id TEXT NOT NULL, committed_at TEXT NOT NULL)"),
    ("CREATE INDEX IF NOT EXISTS sgs_coverage_receipts_scope_idx "
    "ON sgs_coverage_receipts (series_code, contract_version, policy_version, requested_start, requested_end)"),
    ("CREATE TABLE IF NOT EXISTS sgs_unit_commits ("
    "operation_id TEXT PRIMARY KEY, extraction_id TEXT NOT NULL, run_id TEXT NOT NULL, unit_id TEXT NOT NULL, "
    "plan_sha256 TEXT NOT NULL, series_code INTEGER NOT NULL, contract_id TEXT NOT NULL, contract_version TEXT NOT NULL, "
    "identity_kind TEXT NOT NULL, scope_role TEXT NOT NULL, policy_version TEXT NOT NULL, requested_start TEXT NOT NULL, "
    "requested_end TEXT NOT NULL, payload_sha256 TEXT NOT NULL, canonical_value_sha256 TEXT NOT NULL, "
        "observed_count INTEGER NOT NULL CHECK (observed_count > 0), reference_start TEXT NOT NULL, "
        "reference_end TEXT NOT NULL, "
    "captured_at TEXT NOT NULL)"),
    "ALTER TABLE source_extractions ADD COLUMN network_payload_sha256 TEXT",
    "ALTER TABLE source_extractions ADD COLUMN network_route TEXT",
    "ALTER TABLE source_extractions ADD COLUMN network_provider TEXT",
    "ALTER TABLE source_extractions ADD COLUMN network_contract TEXT",
    "ALTER TABLE source_extractions ADD COLUMN network_scope_id TEXT",
    "ALTER TABLE source_extractions ADD COLUMN network_identity TEXT",
    "ALTER TABLE source_extractions ADD COLUMN network_coverage TEXT",
    "ALTER TABLE source_extractions ADD COLUMN network_captured_at TEXT",
    "ALTER TABLE source_extractions ADD COLUMN network_reference_count INTEGER",
)

_POSTGRES = (
    ("CREATE TABLE IF NOT EXISTS sgs_refresh_plans ("
    "run_id TEXT PRIMARY KEY, plan_version TEXT NOT NULL, as_of TEXT NOT NULL, "
    "plan_json TEXT NOT NULL, plan_sha256 TEXT NOT NULL, created_at TEXT NOT NULL)"),
    ("CREATE TABLE IF NOT EXISTS sgs_refresh_plan_units ("
    "run_id TEXT NOT NULL REFERENCES sgs_refresh_plans(run_id), unit_id TEXT NOT NULL, ordinal INTEGER NOT NULL, "
    "role TEXT NOT NULL, series_code INTEGER NOT NULL, indicator_key TEXT NOT NULL, group_name TEXT NOT NULL, "
    "freshness_days INTEGER NOT NULL, policy_version TEXT NOT NULL, "
    "contract_version TEXT NOT NULL, identity_kind TEXT NOT NULL, unit TEXT NOT NULL, method TEXT NOT NULL, "
    "frequency TEXT NOT NULL, start_date TEXT NOT NULL, end_date TEXT NOT NULL, "
    "status TEXT NOT NULL DEFAULT 'reserved', last_error_code TEXT, updated_at TEXT NOT NULL, "
    "PRIMARY KEY (run_id, unit_id))"),
    ("CREATE INDEX IF NOT EXISTS sgs_refresh_plan_units_status_idx "
    "ON sgs_refresh_plan_units (status, role, series_code, start_date)"),
    ("CREATE TABLE IF NOT EXISTS sgs_archive_cursors ("
    "source TEXT PRIMARY KEY, last_series_code INTEGER, updated_at TEXT NOT NULL)"),
    ("CREATE TABLE IF NOT EXISTS sgs_coverage_receipts ("
    "operation_id TEXT PRIMARY KEY, run_id TEXT NOT NULL, unit_id TEXT NOT NULL, source TEXT NOT NULL, "
    "series_code INTEGER NOT NULL, contract_id TEXT NOT NULL, contract_version TEXT NOT NULL, "
    "identity_kind TEXT NOT NULL, scope_role TEXT NOT NULL, policy_version TEXT NOT NULL, "
    "requested_start TEXT NOT NULL, requested_end TEXT NOT NULL, payload_sha256 TEXT NOT NULL, "
    "observed_count INTEGER NOT NULL CHECK (observed_count > 0), captured_at TEXT NOT NULL, "
    "reference_start TEXT NOT NULL, reference_end TEXT NOT NULL, "
    "business_receipt_id TEXT NOT NULL, committed_at TEXT NOT NULL)"),
    ("CREATE INDEX IF NOT EXISTS sgs_coverage_receipts_scope_idx "
    "ON sgs_coverage_receipts (series_code, contract_version, policy_version, requested_start, requested_end)"),
    ("CREATE TABLE IF NOT EXISTS sgs_unit_commits ("
    "operation_id TEXT PRIMARY KEY, extraction_id TEXT NOT NULL, run_id TEXT NOT NULL, unit_id TEXT NOT NULL, "
    "plan_sha256 TEXT NOT NULL, series_code INTEGER NOT NULL, contract_id TEXT NOT NULL, contract_version TEXT NOT NULL, "
    "identity_kind TEXT NOT NULL, scope_role TEXT NOT NULL, policy_version TEXT NOT NULL, requested_start TEXT NOT NULL, "
    "requested_end TEXT NOT NULL, payload_sha256 TEXT NOT NULL, canonical_value_sha256 TEXT NOT NULL, "
        "observed_count INTEGER NOT NULL CHECK (observed_count > 0), reference_start TEXT NOT NULL, "
        "reference_end TEXT NOT NULL, "
    "captured_at TEXT NOT NULL)"),
    "ALTER TABLE source_extractions ADD COLUMN IF NOT EXISTS network_payload_sha256 TEXT",
    "ALTER TABLE source_extractions ADD COLUMN IF NOT EXISTS network_route TEXT",
    "ALTER TABLE source_extractions ADD COLUMN IF NOT EXISTS network_provider TEXT",
    "ALTER TABLE source_extractions ADD COLUMN IF NOT EXISTS network_contract TEXT",
    "ALTER TABLE source_extractions ADD COLUMN IF NOT EXISTS network_scope_id TEXT",
    "ALTER TABLE source_extractions ADD COLUMN IF NOT EXISTS network_identity TEXT",
    "ALTER TABLE source_extractions ADD COLUMN IF NOT EXISTS network_coverage TEXT",
    "ALTER TABLE source_extractions ADD COLUMN IF NOT EXISTS network_captured_at TEXT",
    "ALTER TABLE source_extractions ADD COLUMN IF NOT EXISTS network_reference_count INTEGER",
)

SQLITE_STATEMENTS: Final = _SQLITE
POSTGRES_STATEMENTS: Final = _POSTGRES
