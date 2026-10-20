import argparse
import json
from pathlib import Path

from macro_score.retrospective import reconstruct_di_ledger


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Calculate retrospective DI macro components from a local isolated ledger"
    )
    parser.add_argument("--sqlite", required=True, type=Path, help="absolute isolated SQLite ledger")
    parser.add_argument("--execute", action="store_true", help="append retrospective values; default is dry-run")
    parser.add_argument("--focus-sqlite", type=Path, help="optional existing read-only SQLite source for Focus vintages")
    args = parser.parse_args()
    print(json.dumps(
        reconstruct_di_ledger(args.sqlite, execute=args.execute, focus_sqlite=args.focus_sqlite), indent=2,
    ))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
