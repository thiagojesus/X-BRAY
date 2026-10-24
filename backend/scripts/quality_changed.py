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
)


def run(command: list[str]) -> None:
    completed = subprocess.run(command, cwd=BACKEND_ROOT, check=False)
    if completed.returncode != 0:
        raise SystemExit(completed.returncode)


def main() -> None:
    run([sys.executable, "-m", "ruff", "check", *STRICT_PATHS])
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
