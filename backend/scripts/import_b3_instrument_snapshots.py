"""Audit/import B3 bvmf.100.02 instrument snapshots into isolated SQLite."""

import argparse
import json
from pathlib import Path

from macro_score.b3_instruments import import_instrument_files


def main() -> int:
    parser = argparse.ArgumentParser(description="Audit or append B3 DI1 instrument snapshots")
    parser.add_argument("--sqlite", required=True, type=Path, help="isolated absolute SQLite .db target")
    parser.add_argument("--execute", action="store_true", help="append validated snapshots; default is audit only")
    parser.add_argument("inputs", nargs="+", type=Path, help="B3 bvmf.100.02 XML files or directories")
    args = parser.parse_args()
    paths = sorted({
        path for item in args.inputs
        for path in (item.rglob("*.xml") if item.is_dir() else (item,))
        if path.is_file() and path.suffix.lower() == ".xml"
    })
    print(json.dumps(import_instrument_files(args.sqlite, paths, execute=args.execute), indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
