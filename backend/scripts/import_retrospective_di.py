import argparse
import json
from datetime import date
from pathlib import Path

from macro_score.retrospective_di import import_local_files, inventory, parse_local_files


def main() -> int:
    parser = argparse.ArgumentParser(description="Inventory/import local B3 DI XML or ZIP files")
    parser.add_argument("--sqlite", required=True, type=Path, help="explicit isolated SQLite target (.db)")
    parser.add_argument("--start", required=True, type=date.fromisoformat)
    parser.add_argument("--end", required=True, type=date.fromisoformat)
    parser.add_argument("inputs", nargs="+", type=Path, help="local XML/ZIP files or directories")
    parser.add_argument("--execute", action="store_true", help="persist; default is inventory only")
    args = parser.parse_args()
    paths = sorted({
        path for item in args.inputs
        for path in (item.rglob("*") if item.is_dir() else (item,))
        if path.is_file() and path.suffix.lower() in {".xml", ".zip"}
    })
    rows = parse_local_files(paths)
    report = inventory(rows, start=args.start, end=args.end)
    report.update(import_local_files(args.sqlite, paths, execute=args.execute))
    print(json.dumps(report, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
