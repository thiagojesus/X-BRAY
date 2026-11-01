from typing import Final

MIGRATION_NAME: Final = "023_admin_read_indexes"

SQLITE_STATEMENTS: Final[tuple[str, ...]] = (
    "CREATE INDEX IF NOT EXISTS refresh_runs_created_cursor_idx ON refresh_runs (julianday(created_at) DESC, run_id DESC)",
    "CREATE INDEX IF NOT EXISTS refresh_runs_state_created_idx ON refresh_runs (state, julianday(created_at) DESC, run_id DESC)",
    "CREATE INDEX IF NOT EXISTS sgs_unit_commits_series_capture_idx ON sgs_unit_commits (series_code, captured_at DESC, operation_id DESC)",
    "CREATE INDEX IF NOT EXISTS sgs_coverage_receipts_admin_candidates_idx ON sgs_coverage_receipts (series_code, requested_start, requested_end, captured_at, operation_id) WHERE source = 'bcb_sgs' AND scope_role IN ('archive_backfill','archive_reconcile')",
    "CREATE INDEX IF NOT EXISTS source_extractions_provider_failure_idx ON source_extractions (source, status, started_at DESC, extraction_id DESC)",
)

POSTGRES_STATEMENTS: Final[tuple[str, ...]] = (
    "CREATE INDEX IF NOT EXISTS sgs_unit_commits_series_capture_idx ON sgs_unit_commits (series_code, captured_at DESC, operation_id DESC)",
    "CREATE INDEX IF NOT EXISTS sgs_coverage_receipts_admin_candidates_idx ON sgs_coverage_receipts (series_code, requested_start, requested_end, captured_at, operation_id) WHERE source = 'bcb_sgs' AND scope_role IN ('archive_backfill','archive_reconcile')",
    "CREATE INDEX IF NOT EXISTS source_extractions_provider_failure_idx ON source_extractions (source, status, started_at DESC, extraction_id DESC)",
)
