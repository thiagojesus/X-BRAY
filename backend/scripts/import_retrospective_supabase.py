"""Dry-run by default; transport the reviewed retrospective import via Supabase CLI."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from macro_score.production_import import EXPECTED_TARGET
from macro_score.supabase_cli_import import (
    SupabaseCliError,
    discard_staging_batch,
    run_supabase_import,
)


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--di-source-db", type=Path)
    parser.add_argument("--non-di-source-db", type=Path)
    parser.add_argument("--focus-source-db", type=Path)
    parser.add_argument("--target-project", required=True)
    parser.add_argument("--execute", action="store_true")
    parser.add_argument("--confirm-production-target")
    parser.add_argument("--expected-bundle-sha256")
    parser.add_argument("--expected-total-rows", type=int)
    parser.add_argument("--backup-path", type=Path)
    parser.add_argument(
        "--discard-staging-batch",
        help="remove only private staging for this SHA-256 batch",
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    if args.target_project != EXPECTED_TARGET:
        print("refused: unexpected production target", file=sys.stderr)
        return 2
    try:
        if args.discard_staging_batch:
            if args.execute or any(
                (args.di_source_db, args.non_di_source_db, args.focus_source_db)
            ):
                raise ValueError("staging cleanup is a separate operation")
            report = discard_staging_batch(args.discard_staging_batch)
        else:
            if not all(
                (args.di_source_db, args.non_di_source_db, args.focus_source_db)
            ):
                raise ValueError("all source database paths are required")
            report = run_supabase_import(
                di_db=args.di_source_db,
                non_di_db=args.non_di_source_db,
                focus_db=args.focus_source_db,
                target=args.target_project,
                execute=args.execute,
                confirm_target=args.confirm_production_target,
                expected_sha256=args.expected_bundle_sha256,
                expected_total_rows=args.expected_total_rows,
                backup_path=args.backup_path,
                repo_root=Path(__file__).resolve().parents[2],
            )
    except (ValueError, SupabaseCliError, OSError) as error:
        print(
            json.dumps(
                {
                    "error": "supabase_import_refused_or_rolled_back",
                    "error_type": type(error).__name__,
                }
            ),
            file=sys.stderr,
        )
        return 1
    print(json.dumps(report, sort_keys=True, indent=2, allow_nan=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
