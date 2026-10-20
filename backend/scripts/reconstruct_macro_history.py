import argparse
import json
from datetime import date
from pathlib import Path

from macro_score.retrospective import reconstruct


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Reconstruct official non-DI macro reference history"
    )
    parser.add_argument(
        "--sqlite", required=True, type=Path, help="explicit local SQLite target (.db)"
    )
    parser.add_argument("--start", required=True, type=date.fromisoformat)
    parser.add_argument("--end", required=True, type=date.fromisoformat)
    parser.add_argument(
        "--execute",
        action="store_true",
        help="persist; without this flag performs a dry-run",
    )
    args = parser.parse_args()
    print(
        json.dumps(
            reconstruct(
                args.sqlite, start=args.start, end=args.end, dry_run=not args.execute
            ),
            indent=2,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
