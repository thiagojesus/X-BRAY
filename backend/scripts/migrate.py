"""Inspect or explicitly apply the application database migrations."""

from __future__ import annotations

import argparse
import re
import sqlite3
import sys
from urllib.parse import urlsplit

import psycopg

from db import store
from db.migration_steps import MIGRATIONS


def _target_label(database_url: str) -> str:
    if not database_url:
        return "SQLite local database"
    host = urlsplit(database_url).hostname or "unknown-host"
    match = re.search(r"([a-z0-9]{20,})\.supabase\.co$", host, re.IGNORECASE)
    reference = match.group(1) if match else "unrecognized-host"
    return f"PostgreSQL Supabase project ref {reference} (credentials redacted)"


def _installed_versions() -> set[str]:
    if not store.uses_postgres() and not store.DB_PATH.is_file():
        return set()
    return store.read_migration_versions()


def _status() -> tuple[list[str], list[str]]:
    try:
        installed: set[str] = _installed_versions()
    except (psycopg.Error, sqlite3.Error) as error:
        # Do not echo driver exceptions: they can contain connection details.
        if "does not exist" not in str(error).lower() and "no such table" not in str(error).lower():
            raise RuntimeError("Could not read migration journal; connection details are redacted") from None
        installed = set()
    expected = {name for name, _sqlite, _postgres in MIGRATIONS}
    return sorted(expected - installed), sorted(installed - expected)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    action = parser.add_mutually_exclusive_group()
    action.add_argument("--check", action="store_true", help="read-only compatibility check (default)")
    action.add_argument("--plan", action="store_true", help="read-only migration plan (default)")
    action.add_argument("--apply", action="store_true", help="explicitly apply pending migrations")
    args = parser.parse_args(argv)
    try:
        pending, unknown = _status()
        print(f"Target: {_target_label(store.DATABASE_URL)}")
        print("Pending: " + (", ".join(pending) if pending else "none"))
        if unknown:
            print("Unknown installed versions: " + ", ".join(unknown))
            return 2
        if args.apply:
            if pending:
                store.init_db()
                remaining, unknown_after = _status()
                if remaining or unknown_after:
                    print("Migration post-check failed; inspect database state before release.", file=sys.stderr)
                    return 2
            print("Schema migrations are applied and verified.")
            return 0
        if pending:
            print("Read-only check: incompatible schema; no database changes made.")
            return 2
        print("Read-only check: schema is compatible; no database changes made.")
        return 0
    except (psycopg.Error, sqlite3.Error, RuntimeError) as error:
        print(f"Migration command failed: {type(error).__name__}; connection details redacted", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
