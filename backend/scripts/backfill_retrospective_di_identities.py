import argparse
import json
from pathlib import Path

from macro_score.retrospective_di import backfill_cached_price_identities


def main() -> int:
    parser = argparse.ArgumentParser(description="Append DI security IDs from local B3 price ZIP cache")
    parser.add_argument("--sqlite", required=True, type=Path, help="isolated absolute SQLite ledger")
    parser.add_argument("--cache-dir", required=True, type=Path)
    parser.add_argument("--execute", action="store_true", help="append identity crosswalk; default audit only")
    args = parser.parse_args()
    archives = sorted(args.cache_dir.glob("SPRD*.zip"))
    print(json.dumps(backfill_cached_price_identities(args.sqlite, archives, execute=args.execute), indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
