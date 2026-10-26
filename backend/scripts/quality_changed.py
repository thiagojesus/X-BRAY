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
    # New Phase 1-3 production modules must be strict-typed, staged or not.
    "db/migration_steps/v015_ingestion_policy.py",
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
)
RUFF_PATHS = (
    "datafetchers/anbima.py",
    "datafetchers/b3_di.py",
    "datafetchers/bcb_sgs.py",
    "datafetchers/focus.py",
    "datafetchers/ibge_sidra.py",
    "datafetchers/tesouro_direto.py",
    "db/store.py",
    "db/migration_steps/__init__.py",
    "db/migration_steps/v015_ingestion_policy.py",
    "ingestion_policy.py",
    "provider_http.py",
    "source_ingestion.py",
    "source_contracts.py",
    "source_candidates.py",
    "shadow_ingestion.py",
    "shadow_cli.py",
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
