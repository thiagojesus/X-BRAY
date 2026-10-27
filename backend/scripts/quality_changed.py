from __future__ import annotations

import subprocess
import sys
from pathlib import Path

BACKEND_ROOT = Path(__file__).resolve().parents[1]
STRICT_PATHS = (
    "domain_contracts.py",
    "provider_hosts.py",
    "provider_http.py",
    "refresh.py",
    "refresh_derived_phase.py",
    "refresh_auth.py",
    "db/migrations.py",
    "db/migration_steps",
    "derived",
    "macro_score",
    "routes/derivados.py",
    "routes/macro_score.py",
    "source_audit",
    "routes/source_audit.py",
    "candidate_acquisition.py",
    "candidate_eligibility.py",
    "canary_acquisition.py",
    "canary_executor.py",
    "source_rollout.py",
    "routes/source_candidates.py",
    # New Phase 1-3 production modules must be strict-typed, staged or not.
    "db/migration_steps/v015_ingestion_policy.py",
    "db/treasury_store.py",
    "db/refresh_attempt.py",
    "ingestion_policy.py",
    "source_contracts.py",
    "source_candidates.py",
    "shadow_ingestion.py",
    "shadow_cli.py",
)
TEST_PATHS = (
    "tests/test_ingestion_policy.py",
    "tests/test_ingestion_policy_collectors.py",
    "tests/test_source_contracts.py",
    "tests/test_source_candidates.py",
    "tests/test_shadow_ingestion.py",
    "tests/test_migrations.py",
    "tests/test_provider_http_bounded.py",
    "tests/test_source_ingestion.py",
    "tests/test_source_ingestion_postgres.py",
    "tests/test_store_sidra.py",
    "tests/test_candidate_acquisition_schema.py",
    "tests/test_candidate_atomic_persistence.py",
    "tests/test_canary_binding_persistence.py",
    "tests/test_candidate_eligibility.py",
    "tests/test_candidate_postgres_integration.py",
    "tests/test_canary_postgres_integration.py",
    "tests/test_candidate_producer_integration.py",
    "tests/test_source_rollout_controls.py",
    "tests/test_canary_acquisition.py",
    "tests/test_canary_executor_integration.py",
    "tests/test_candidate_origin_parsers.py",
    "tests/test_candidate_origin_audit.py",
    "tests/test_source_audit.py",
    "tests/test_stagec_workflow_controls.py",
    "tests/test_treasury_bounded_persistence.py",
    "tests/test_tesouro_refresh.py",
    "tests/test_store_treasury.py",
    "tests/test_refresh_run_execution.py",
    "tests/test_refresh_status.py",
    "tests/test_refresh_run_client.py",
    "tests/test_refresh_run_claims.py",
    "tests/test_sgs_bounded_refresh.py",
)
RUFF_PATHS = (
    "datafetchers/anbima.py",
    "datafetchers/b3_di.py",
    "datafetchers/bcb_sgs.py",
    "datafetchers/focus.py",
    "datafetchers/ibge_sidra.py",
    "datafetchers/tesouro_direto.py",
    "db/store.py",
    "tests/test_source_ingestion_postgres.py",
    "db/refresh_run_items.py",
    "db/treasury_store.py",
    "db/migration_steps/__init__.py",
    "db/migration_steps/v015_ingestion_policy.py",
    "db/migration_steps/v016_candidate_acquisition.py",
    "db/migration_steps/v017_treasury_cohort_receipts.py",
    "db/migration_steps/v018_refresh_attempt_lifecycle.py",
    "db/refresh_attempt.py",
    "refresh.py",
    "refresh_manifest.py",
    "refresh_reporting.py",
    "refresh_run_execution.py",
    "source_ingestion.py",
    "provider_http.py",
    "refresh_run_tasks.py",
    "candidate_acquisition.py",
    "candidate_eligibility.py",
    "canary_acquisition.py",
    "db/sidra_store.py",
    "scripts/quality_changed.py",
    "ingestion_policy.py",
    "provider_http.py",
    "source_ingestion.py",
    "source_contracts.py",
    "source_candidates.py",
    "shadow_ingestion.py",
    "shadow_cli.py",
    "routes/source_candidates.py",
    "source_rollout.py",
    "source_audit/candidate_origins.py",
    "source_audit/auditor.py",
    "source_audit/catalog.py",
    *TEST_PATHS,
)


def run(command: list[str]) -> None:
    completed = subprocess.run(command, cwd=BACKEND_ROOT, check=False)
    if completed.returncode != 0:
        raise SystemExit(completed.returncode)


def main() -> None:
    run([sys.executable, "-m", "ruff", "check", *RUFF_PATHS])
    run([
        sys.executable,
        "-m",
        "basedpyright",
        "--pythonpath",
        sys.executable,
        "--level",
        "error",
        *STRICT_PATHS,
    ])


if __name__ == "__main__":
    main()
