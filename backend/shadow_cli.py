"""Manual CLI for read-only Phase 3 equivalence shadow runs."""

from __future__ import annotations

import argparse
import json
import re
import sys
from datetime import date
from pathlib import Path

from shadow_ingestion import run_shadow
from source_candidates import CANDIDATE_REGISTRY

_AS_OF_PATTERN = re.compile(r"^[0-9]{4}-[0-9]{2}-[0-9]{2}$")


def _parse_as_of(value: str) -> date:
    if _AS_OF_PATTERN.fullmatch(value) is None:
        raise argparse.ArgumentTypeError("as-of must use YYYY-MM-DD")
    try:
        return date.fromisoformat(value)
    except ValueError as error:
        raise argparse.ArgumentTypeError("as-of must be a valid calendar date") from error


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="xbray-shadow")
    commands = parser.add_subparsers(dest="command", required=True)
    shadow = commands.add_parser("shadow", help="run a read-only candidate comparison")
    shadow.add_argument("--candidate", required=True, choices=sorted(CANDIDATE_REGISTRY))
    shadow.add_argument("--as-of", required=True, type=_parse_as_of)
    shadow.add_argument("--artifact", required=True, type=Path)
    return parser


def main(argv: list[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    try:
        result = run_shadow(args.candidate, args.as_of, args.artifact)
    except (ValueError, OSError) as error:
        print(json.dumps({"status": "error", "error_type": type(error).__name__}), file=sys.stderr)
        return 2
    print(json.dumps({
        "status": result.get("status"),
        "candidate_id": args.candidate,
        "artifact": str(args.artifact.expanduser()),
        "production_eligible": False,
    }, sort_keys=True))
    return 0 if result.get("status") == "evidence_passed" else 2


if __name__ == "__main__":
    raise SystemExit(main())
