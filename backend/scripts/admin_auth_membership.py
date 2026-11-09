"""Backend-only, auditable operator tool; never exposed as an HTTP route."""

from __future__ import annotations

import argparse
from uuid import UUID

from db import admin_auth
from db.store import check_db_schema


def _uuid(value: str) -> str:
    try:
        return str(UUID(value))
    except ValueError as exc:
        raise argparse.ArgumentTypeError("must be a UUID") from exc


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    subparsers = parser.add_subparsers(dest="command", required=True)
    for command in ("grant", "revoke"):
        subparser = subparsers.add_parser(command)
        subparser.add_argument("user_id", type=_uuid)
        subparser.add_argument("--actor-id", type=_uuid, required=True)
        subparser.add_argument("--reason", required=True, choices=("operator-approved", "operator-revoked"))
    cleanup_parser = subparsers.add_parser("cleanup")
    cleanup_parser.add_argument("--batch-size", type=int, default=200, choices=range(1, 1001))
    args = parser.parse_args()
    check_db_schema()
    if args.command == "grant":
        admin_auth.grant_admin(args.user_id, granted_by=args.actor_id, reason=args.reason)
        print("membership grant recorded")
    elif args.command == "revoke":
        changed = admin_auth.revoke_admin(args.user_id, granted_by=args.actor_id, reason=args.reason)
        print("membership revoked" if changed else "no active membership")
    else:
        print(admin_auth.cleanup(batch_size=args.batch_size))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
