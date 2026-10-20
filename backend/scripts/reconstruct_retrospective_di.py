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
    args = parser.parse_args()
    print(json.dumps(
        reconstruct_di_ledger(args.sqlite, execute=args.execute), indent=2,
    ))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
