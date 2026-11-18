from __future__ import annotations

import ast
import hashlib
import os
import subprocess
import sys
import tarfile
from io import BytesIO
from pathlib import Path
from tempfile import TemporaryDirectory
from urllib.parse import urlsplit

import psycopg
import pytest
from psycopg import sql

from db.migration_steps.v024_operational_rls import TABLES as OPERATIONAL_TABLES

OLD_COMMIT = "913193a40e8eb9926d45bc88cbcfd359c56796a9"
OLD_SOURCE_SHA1 = {
    "backend/main.py": "2a3ce9916c7cfadb2d56487e1fdfb608efe52c14",
    "backend/db/store.py": "477a90a7efae955f4800bde5a0ae23dab95f7c7b",
    "backend/db/migrations.py": "bd7a88d61da12c0ce3f27e38f5b4f3f3ec6ba406",
    "backend/db/migration_steps/__init__.py": "adf112b42eebc2f79aff0f283d76100d37ba2e86",
}
BACKEND_ROOT = Path(__file__).resolve().parents[1]
REPO_ROOT = BACKEND_ROOT.parent
DATABASE_ENV = "COMPAT_TEST_DATABASE_URL"
LOOPBACK_HOSTS = {"127.0.0.1", "localhost", "::1"}
SENTINEL_RUN = "old-startup-024-compat"
SENTINEL_SGS_CODE = 987654
if os.environ.get("XBRAY_024_COMPAT_CI") == "1" and not os.environ.get(DATABASE_ENV):
    raise RuntimeError(f"dedicated compatibility CI requires {DATABASE_ENV}")

pytestmark = pytest.mark.skipif(
    not os.environ.get(DATABASE_ENV),
    reason=f"requires the dedicated loopback PostgreSQL 17 {DATABASE_ENV}",
)


def _database_url() -> str:
    dsn = os.environ[DATABASE_ENV]
    parsed = urlsplit(dsn)
    if (
        parsed.scheme not in {"postgres", "postgresql"}
        or parsed.hostname not in LOOPBACK_HOSTS
        or parsed.path.lstrip("/") != "xbray_compat_test"
    ):
        pytest.fail(f"{DATABASE_ENV} must target loopback database xbray_compat_test")
    return dsn


def _run(command: list[str], *, cwd: Path, dsn: str, temp: Path) -> None:
    env = {
        "PATH": os.environ.get("PATH", ""),
        "HOME": os.environ.get("HOME", str(temp)),
        "DATABASE_URL": dsn,
        "XBRAY_SQLITE_PATH": str(temp / "unused.sqlite"),
        "PYTHONPATH": str(cwd),
    }
    result = subprocess.run(
        command,
        cwd=cwd,
        env=env,
        capture_output=True,
        text=True,
        timeout=45,
        check=False,
    )
    assert result.returncode == 0, (
        f"isolated startup/migration subprocess failed ({result.returncode}); "
        f"stdout={result.stdout[-2000:]!r}; stderr={result.stderr[-2000:]!r}"
    )


def _extract_pinned_source(destination: Path) -> Path:
    paths = (
        "backend/main.py",
        "backend/config.py",
        "backend/indicator_catalog.py",
        "backend/domain_contracts.py",
        "backend/db",
    )
    archive = subprocess.run(
        ["git", "archive", "--format=tar", OLD_COMMIT, *paths],
        cwd=REPO_ROOT,
        capture_output=True,
        timeout=20,
        check=True,
    )
    destination.mkdir()
    with tarfile.open(fileobj=BytesIO(archive.stdout), mode="r:") as bundle:
        for member in bundle.getmembers():
            member_path = Path(member.name)
            if member_path.is_absolute() or ".." in member_path.parts:
                raise AssertionError("pinned source archive contains an unsafe path")
        bundle.extractall(destination, filter="data")

    backend = destination / "backend"
    for relative_path, expected_hash in OLD_SOURCE_SHA1.items():
        source = backend / relative_path.removeprefix("backend/")
        assert hashlib.sha1(source.read_bytes()).hexdigest() == expected_hash
    tree = ast.parse((backend / "main.py").read_text(encoding="utf-8"))
    lifespan = next(
        node
        for node in tree.body
        if isinstance(node, ast.AsyncFunctionDef) and node.name == "lifespan"
    )
    assert len(lifespan.body) == 2
    init_call, yield_statement = lifespan.body
    assert (
        isinstance(init_call, ast.Expr)
        and isinstance(init_call.value, ast.Call)
        and isinstance(init_call.value.func, ast.Name)
        and init_call.value.func.id == "init_db"
    )
    assert isinstance(yield_statement, ast.Expr) and isinstance(yield_statement.value, ast.Yield)
    return backend


def _prepare_database(conn: psycopg.Connection[object]) -> None:
    for role in ("anon", "authenticated", "compat_browser_inherited"):
        conn.execute(
            sql.SQL("CREATE ROLE {} NOLOGIN INHERIT").format(sql.Identifier(role))
        )
    conn.execute("GRANT anon TO compat_browser_inherited")
    conn.execute("CREATE ROLE service_role NOLOGIN INHERIT BYPASSRLS")

    for schema in ("auth", "storage", "extensions"):
        conn.execute(sql.SQL("CREATE SCHEMA {}").format(sql.Identifier(schema)))
        conn.execute(
            sql.SQL("CREATE TABLE {}.acl_sentinel (id integer PRIMARY KEY)").format(
                sql.Identifier(schema)
            )
        )
        conn.execute(
            sql.SQL("GRANT USAGE ON SCHEMA {} TO anon, authenticated").format(
                sql.Identifier(schema)
            )
        )
        conn.execute(
            sql.SQL("GRANT SELECT ON TABLE {}.acl_sentinel TO anon, authenticated").format(
                sql.Identifier(schema)
            )
        )
        conn.execute(
            sql.SQL(
                "ALTER DEFAULT PRIVILEGES FOR ROLE postgres IN SCHEMA {} "
                "GRANT SELECT ON TABLES TO anon"
            ).format(sql.Identifier(schema))
        )
        conn.execute(
            sql.SQL(
                "ALTER DEFAULT PRIVILEGES FOR ROLE postgres IN SCHEMA {} "
                "GRANT USAGE, SELECT ON SEQUENCES TO anon"
            ).format(sql.Identifier(schema))
        )

    conn.execute(
        "ALTER DEFAULT PRIVILEGES FOR ROLE postgres IN SCHEMA public "
        "GRANT ALL ON TABLES TO anon, authenticated"
    )
    conn.execute(
        "ALTER DEFAULT PRIVILEGES FOR ROLE postgres IN SCHEMA public "
        "GRANT ALL ON SEQUENCES TO anon, authenticated"
    )
    conn.execute(
        "ALTER DEFAULT PRIVILEGES FOR ROLE postgres IN SCHEMA public "
        "GRANT ALL ON TABLES TO PUBLIC"
    )
    conn.execute(
        "ALTER DEFAULT PRIVILEGES FOR ROLE postgres IN SCHEMA public "
        "GRANT ALL ON SEQUENCES TO PUBLIC"
    )


def _seed_pre_migration_acl(conn: psycopg.Connection[object]) -> None:
    conn.execute("GRANT USAGE ON SCHEMA public TO service_role")
    for table in OPERATIONAL_TABLES:
        conn.execute(
            sql.SQL("GRANT ALL PRIVILEGES ON TABLE public.{} TO PUBLIC, anon, authenticated").format(
                sql.Identifier(table)
            )
        )
        conn.execute(
            sql.SQL(
                "GRANT SELECT, INSERT, UPDATE, DELETE ON TABLE public.{} TO service_role"
            ).format(sql.Identifier(table))
        )
        columns = conn.execute(
            "SELECT attname FROM pg_attribute WHERE attrelid = %s::regclass "
            "AND attnum > 0 AND NOT attisdropped ORDER BY attnum LIMIT 1",
            (f"public.{table}",),
        ).fetchone()
        assert columns is not None
        conn.execute(
            sql.SQL("GRANT SELECT ({}) ON TABLE public.{} TO anon, authenticated").format(
                sql.Identifier(columns[0]), sql.Identifier(table)
            )
        )
        sequences = conn.execute(
            "SELECT DISTINCT sequence.relname FROM pg_class AS sequence "
            "JOIN pg_depend AS dependency ON dependency.objid = sequence.oid "
            "JOIN pg_class AS target ON target.oid = dependency.refobjid "
            "JOIN pg_namespace AS namespace ON namespace.oid = target.relnamespace "
            "WHERE sequence.relkind = 'S' AND namespace.nspname = 'public' "
            "AND target.relname = %s",
            (table,),
        ).fetchall()
        for (sequence_name,) in sequences:
            conn.execute(
                sql.SQL(
                    "GRANT ALL PRIVILEGES ON SEQUENCE public.{} TO PUBLIC, anon, authenticated"
                ).format(
                    sql.Identifier(sequence_name)
                )
            )
            conn.execute(
                sql.SQL("GRANT USAGE, SELECT, UPDATE ON SEQUENCE public.{} TO service_role").format(
                    sql.Identifier(sequence_name)
                )
            )


def _external_acl_snapshot(conn: psycopg.Connection[object]) -> tuple[object, ...]:
    schemas = tuple(
        conn.execute(
            "SELECT nspname, nspacl::text FROM pg_namespace "
            "WHERE nspname IN ('auth', 'storage', 'extensions') ORDER BY nspname"
        ).fetchall()
    )
    objects = tuple(
        conn.execute(
            "SELECT namespace.nspname, object.relname, object.relacl::text, "
            "object.relrowsecurity, owner.rolname "
            "FROM pg_class AS object JOIN pg_namespace AS namespace "
            "ON namespace.oid = object.relnamespace "
            "JOIN pg_roles AS owner ON owner.oid = object.relowner "
            "WHERE namespace.nspname IN ('auth', 'storage', 'extensions') "
            "AND object.relkind IN ('r', 'p', 'v', 'm', 'S') "
            "ORDER BY namespace.nspname, object.relname"
        ).fetchall()
    )
    defaults = tuple(
        conn.execute(
            "SELECT namespace.nspname, defaults.defaclobjtype, defaults.defaclacl::text "
            "FROM pg_default_acl AS defaults "
            "JOIN pg_namespace AS namespace ON namespace.oid = defaults.defaclnamespace "
            "JOIN pg_roles AS owner ON owner.oid = defaults.defaclrole "
            "WHERE owner.rolname = 'postgres' "
            "AND namespace.nspname IN ('auth', 'storage', 'extensions') "
            "ORDER BY namespace.nspname, defaults.defaclobjtype"
        ).fetchall()
    )
    return schemas, objects, defaults


def _public_default_acl_snapshot(conn: psycopg.Connection[object]) -> tuple[object, ...]:
    return tuple(
        conn.execute(
            "SELECT defaults.defaclobjtype, defaults.defaclacl::text "
            "FROM pg_default_acl AS defaults "
            "WHERE defaults.defaclrole = 'postgres'::regrole "
            "AND defaults.defaclnamespace = 'public'::regnamespace "
            "AND defaults.defaclobjtype IN ('r', 'S') "
            "ORDER BY defaults.defaclobjtype"
        ).fetchall()
    )


def _admin_acl_snapshot(conn: psycopg.Connection[object]) -> tuple[object, ...]:
    rows = []
    for table in ("admin_memberships", "admin_sessions", "admin_oauth_flows", "admin_auth_events"):
        row = conn.execute(
            "SELECT object.relrowsecurity, object.relacl::text, owner.rolname, "
            "has_table_privilege('anon', object.oid, 'SELECT'), "
            "has_table_privilege('authenticated', object.oid, 'SELECT') "
            "FROM pg_class AS object JOIN pg_namespace AS namespace "
            "ON namespace.oid = object.relnamespace "
            "JOIN pg_roles AS owner ON owner.oid = object.relowner "
            "WHERE namespace.nspname = 'public' AND object.relname = %s",
            (table,),
        ).fetchone()
        assert row is not None, f"missing administrative table: {table}"
        rows.append((table, *row))
    return tuple(rows)


def _assert_browser_privileges(
    conn: psycopg.Connection[object], *, require_rls: bool
) -> dict[str, int]:
    table_privileges = ("SELECT", "INSERT", "UPDATE", "DELETE", "TRUNCATE", "REFERENCES", "TRIGGER")
    column_privileges = ("SELECT", "INSERT", "UPDATE", "REFERENCES")
    sequence_privileges = ("USAGE", "SELECT", "UPDATE")
    roles = ("anon", "authenticated", "compat_browser_inherited")
    counts = {"tables": 0, "rls": 0, "sequences": 0}

    for table in OPERATIONAL_TABLES:
        relation = conn.execute(
            "SELECT object.oid, object.relrowsecurity FROM pg_class AS object "
            "JOIN pg_namespace AS namespace ON namespace.oid = object.relnamespace "
            "WHERE namespace.nspname = 'public' AND object.relname = %s",
            (table,),
        ).fetchone()
        assert relation is not None, f"missing managed table: {table}"
        relation_oid, row_security = relation
        counts["tables"] += 1
        counts["rls"] += bool(row_security)
        assert bool(row_security) is require_rls, f"unexpected RLS state: {table}"

        for role in roles:
            for privilege in table_privileges:
                assert not conn.execute(
                    "SELECT has_table_privilege(%s, %s, %s)",
                    (role, relation_oid, privilege),
                ).fetchone()[0], f"{role} retains {privilege} on {table}"
            for (column,) in conn.execute(
                "SELECT attname FROM pg_attribute WHERE attrelid = %s "
                "AND attnum > 0 AND NOT attisdropped",
                (relation_oid,),
            ).fetchall():
                for privilege in column_privileges:
                    assert not conn.execute(
                        "SELECT has_column_privilege(%s, %s, %s, %s)",
                        (role, relation_oid, column, privilege),
                    ).fetchone()[0], f"{role} retains {privilege} on {table}.{column}"

        sequences = conn.execute(
            "SELECT DISTINCT sequence.oid FROM pg_class AS sequence "
            "JOIN pg_depend AS dependency ON dependency.objid = sequence.oid "
            "JOIN pg_class AS target ON target.oid = dependency.refobjid "
            "JOIN pg_namespace AS namespace ON namespace.oid = target.relnamespace "
            "WHERE sequence.relkind = 'S' AND namespace.nspname = 'public' "
            "AND target.relname = %s",
            (table,),
        ).fetchall()
        for (sequence_oid,) in sequences:
            counts["sequences"] += 1
            for role in roles:
                for privilege in sequence_privileges:
                    assert not conn.execute(
                        "SELECT has_sequence_privilege(%s, %s, %s)",
                        (role, sequence_oid, privilege),
                    ).fetchone()[0], f"{role} retains {privilege} on a sequence for {table}"
    assert counts["tables"] == len(OPERATIONAL_TABLES) == 25
    if require_rls:
        assert counts["sequences"] > 0, "manifest sequence privilege check was vacuous"
    return counts


def _assert_no_future_browser_privileges(
    conn: psycopg.Connection[object], table: str, sequence: str
) -> None:
    roles = ("anon", "authenticated", "compat_browser_inherited")
    for role in roles:
        for privilege in ("SELECT", "INSERT", "UPDATE", "DELETE", "TRUNCATE", "REFERENCES", "TRIGGER"):
            assert not conn.execute(
                "SELECT has_table_privilege(%s, %s, %s)", (role, table, privilege)
            ).fetchone()[0], f"{role} retains {privilege} on future table {table}"
        columns = conn.execute(
            "SELECT attname FROM pg_attribute WHERE attrelid = %s::regclass "
            "AND attnum > 0 AND NOT attisdropped",
            (table,),
        ).fetchall()
        for (column,) in columns:
            for privilege in ("SELECT", "INSERT", "UPDATE", "REFERENCES"):
                assert not conn.execute(
                    "SELECT has_column_privilege(%s, %s, %s, %s)",
                    (role, table, column, privilege),
                ).fetchone()[0], f"{role} retains {privilege} on {table}.{column}"
        for privilege in ("USAGE", "SELECT", "UPDATE"):
            assert not conn.execute(
                "SELECT has_sequence_privilege(%s, %s, %s)",
                (role, sequence, privilege),
            ).fetchone()[0], f"{role} retains {privilege} on future sequence {sequence}"


def test_pinned_old_startup_remains_compatible_after_024() -> None:
    dsn = _database_url()
    with TemporaryDirectory(prefix="xbray-old-startup-024-") as temp_name:
        temp = Path(temp_name)
        old_backend = _extract_pinned_source(temp / "pinned")
        with psycopg.connect(dsn, autocommit=True) as conn:
            version, database, session_user, current_user = conn.execute(
                "SELECT version(), current_database(), session_user, current_user"
            ).fetchone()
            assert "PostgreSQL 17" in version
            assert database == "xbray_compat_test"
            assert session_user == current_user == "postgres"
            _prepare_database(conn)

        _run(
            [sys.executable, "-c", "from db import store; store.init_db()"],
            cwd=old_backend,
            dsn=dsn,
            temp=temp,
        )

        with psycopg.connect(dsn) as conn:
            before_journal = tuple(
                conn.execute(
                    "SELECT version, applied_at FROM public.schema_migrations ORDER BY version"
                ).fetchall()
            )
            assert len(before_journal) == 23
            assert all(not version.startswith("024") for version, _ in before_journal)
            _seed_pre_migration_acl(conn)
            before_external = _external_acl_snapshot(conn)
            before_admin = _admin_acl_snapshot(conn)
            assert conn.execute(
                "SELECT relrowsecurity FROM pg_class "
                "WHERE oid = 'public.refresh_runs'::regclass"
            ).fetchone()[0] is False
            assert conn.execute(
                "SELECT has_table_privilege('anon', 'public.refresh_runs', 'SELECT')"
            ).fetchone()[0]
            assert conn.execute(
                "SELECT has_table_privilege('compat_browser_inherited', "
                "'public.refresh_runs', 'SELECT')"
            ).fetchone()[0]
            for table in OPERATIONAL_TABLES:
                assert conn.execute(
                    "SELECT relrowsecurity FROM pg_class "
                    "WHERE oid = %s::regclass",
                    (f"public.{table}",),
                ).fetchone()[0] is False
            conn.execute(
                "INSERT INTO public.refresh_runs "
                "(run_id, cycle_id, state, force, manifest_version, created_at, updated_at) "
                "VALUES (%s, %s, 'succeeded', 0, %s, %s, %s)",
                (SENTINEL_RUN, "compat-cycle", "fixture", "2000-01-01", "2000-01-01"),
            )
            conn.execute(
                "INSERT INTO public.refresh_work_items "
                "(run_id, item_key, task, task_order, status, created_at, updated_at) "
                "VALUES (%s, %s, %s, 0, 'succeeded', %s, %s)",
                (SENTINEL_RUN, "sentinel", "compat", "2000-01-01", "2000-01-01"),
            )
            conn.execute(
                "INSERT INTO public.sgs (series_code, date, value, fetched_at) "
                "VALUES (%s, %s, %s, %s)",
                (SENTINEL_SGS_CODE, "2099-12-31", 1234.5, "2000-01-01"),
            )
            run_sentinel = conn.execute(
                "SELECT * FROM public.refresh_runs WHERE run_id = %s", (SENTINEL_RUN,)
            ).fetchone()
            item_sentinel = conn.execute(
                "SELECT * FROM public.refresh_work_items WHERE run_id = %s", (SENTINEL_RUN,)
            ).fetchone()
            sgs_sentinel = conn.execute(
                "SELECT * FROM public.sgs WHERE series_code = %s AND date = %s",
                (SENTINEL_SGS_CODE, "2099-12-31"),
            ).fetchone()
            conn.commit()

        _run(
            [sys.executable, "scripts/migrate.py", "--apply"],
            cwd=BACKEND_ROOT,
            dsn=dsn,
            temp=temp,
        )

        with psycopg.connect(dsn) as conn:
            after_journal = tuple(
                conn.execute(
                    "SELECT version, applied_at FROM public.schema_migrations ORDER BY version"
                ).fetchall()
            )
            assert len(after_journal) == 24
            assert after_journal[:-1] == before_journal
            assert after_journal[-1][0] == "024_operational_rls"
            before_public_defaults = _public_default_acl_snapshot(conn)
            assert conn.execute(
                "SELECT * FROM public.refresh_runs WHERE run_id = %s", (SENTINEL_RUN,)
            ).fetchone() == run_sentinel
            assert conn.execute(
                "SELECT * FROM public.refresh_work_items WHERE run_id = %s", (SENTINEL_RUN,)
            ).fetchone() == item_sentinel
            assert conn.execute(
                "SELECT * FROM public.sgs WHERE series_code = %s AND date = %s",
                (SENTINEL_SGS_CODE, "2099-12-31"),
            ).fetchone() == sgs_sentinel
            _assert_browser_privileges(conn, require_rls=True)
            assert _external_acl_snapshot(conn) == before_external
            assert _admin_acl_snapshot(conn) == before_admin
            assert conn.execute(
                "SELECT has_table_privilege('service_role', 'public.refresh_runs', 'SELECT')"
            ).fetchone()[0]
            conn.execute("SET ROLE service_role")
            assert conn.execute(
                "SELECT count(*) FROM public.refresh_runs WHERE run_id = %s",
                (SENTINEL_RUN,),
            ).fetchone()[0] == 1
            update = conn.execute(
                "UPDATE public.refresh_work_items SET record_count = record_count + 1 "
                "WHERE run_id = %s AND item_key = 'sentinel'",
                (SENTINEL_RUN,),
            )
            assert update.rowcount == 1
            conn.rollback()

        _run(
            [sys.executable, "-c", "from db import store; store.init_db()"],
            cwd=old_backend,
            dsn=dsn,
            temp=temp,
        )

        with psycopg.connect(dsn) as conn:
            assert tuple(
                conn.execute(
                    "SELECT version, applied_at FROM public.schema_migrations ORDER BY version"
                ).fetchall()
            ) == after_journal
            assert conn.execute(
                "SELECT * FROM public.refresh_runs WHERE run_id = %s", (SENTINEL_RUN,)
            ).fetchone() == run_sentinel
            assert conn.execute(
                "SELECT * FROM public.refresh_work_items WHERE run_id = %s", (SENTINEL_RUN,)
            ).fetchone() == item_sentinel
            assert conn.execute(
                "SELECT * FROM public.sgs WHERE series_code = %s AND date = %s",
                (SENTINEL_SGS_CODE, "2099-12-31"),
            ).fetchone() == sgs_sentinel
            counts = _assert_browser_privileges(conn, require_rls=True)
            assert _external_acl_snapshot(conn) == before_external
            assert _admin_acl_snapshot(conn) == before_admin
            assert conn.execute(
                "SELECT applied_at FROM public.schema_migrations "
                "WHERE version = '024_operational_rls'"
            ).fetchone()[0] == after_journal[-1][1]
            assert _public_default_acl_snapshot(conn) == before_public_defaults
            assert conn.execute(
                "SELECT has_table_privilege('service_role', 'public.refresh_runs', 'SELECT')"
            ).fetchone()[0]
            assert counts["tables"] == 25
            with conn.transaction():
                conn.execute(
                    "CREATE TABLE public.future_default_sentinel "
                    "(id BIGSERIAL PRIMARY KEY, payload text)"
                )
                sequence = conn.execute(
                    "SELECT pg_get_serial_sequence("
                    "'public.future_default_sentinel', 'id')"
                ).fetchone()[0]
                assert sequence is not None
                _assert_no_future_browser_privileges(
                    conn,
                    "public.future_default_sentinel",
                    sequence,
                )
                conn.execute("DROP TABLE public.future_default_sentinel")
