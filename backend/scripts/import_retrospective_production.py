"""Dry-run by default; append audited historical data to the confirmed production DB."""

from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path

from macro_score.production_import import EXPECTED_TARGET, rollback_import, run_import


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--di-source-db", type=Path)
    parser.add_argument("--non-di-source-db", type=Path)
    parser.add_argument("--focus-source-db", type=Path)
    parser.add_argument("--target-project", required=True)
    parser.add_argument("--execute", action="store_true", help="commit after exact dry-run confirmation")
    parser.add_argument("--confirm-production-target", help="must exactly equal xbray-api")
    parser.add_argument("--expected-bundle-sha256")
    parser.add_argument("--expected-total-rows", type=int)
    parser.add_argument("--backup-path", type=Path,
                        help="new absolute gzip JSON backup path outside the repository")
    parser.add_argument("--rollback-batch", help="reverse only rows uniquely journaled to this batch")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    if args.target_project != EXPECTED_TARGET:
        print("refused: unexpected production target", file=sys.stderr)
        return 2
    if args.execute and args.confirm_production_target != EXPECTED_TARGET:
        print("refused: explicit production target confirmation is required", file=sys.stderr)
        return 2
    try:
        if args.rollback_batch:
            if not args.execute or args.backup_path is None:
                raise ValueError("rollback requires --execute and a new --backup-path")
            report = rollback_import(
                database_url=os.environ.get("DATABASE_URL", ""), target=args.target_project,
                batch_id=args.rollback_batch, backup_path=args.backup_path,
                repo_root=Path(__file__).resolve().parents[2],
            )
        else:
            if not all((args.di_source_db, args.non_di_source_db, args.focus_source_db)):
                raise ValueError("all three source database paths are required for import")
            report = run_import(
                di_db=args.di_source_db,
                non_di_db=args.non_di_source_db,
                focus_db=args.focus_source_db,
                database_url=os.environ.get("DATABASE_URL", ""),
                target=args.target_project,
                execute=args.execute,
                expected_sha256=args.expected_bundle_sha256,
                expected_total_rows=args.expected_total_rows,
                backup_path=args.backup_path,
                repo_root=Path(__file__).resolve().parents[2],
            )
    except Exception as error:
        # Driver exception strings may contain connection details; emit only a type and safe status.
        print(json.dumps({"error": "import_refused_or_rolled_back", "error_type": type(error).__name__}),
              file=sys.stderr)
        return 1
    print(json.dumps(report, sort_keys=True, indent=2, allow_nan=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
