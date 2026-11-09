from __future__ import annotations

import os
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone
from urllib.parse import urlparse
from uuid import uuid4

import psycopg
import pytest
from psycopg.conninfo import make_conninfo
from psycopg.errors import InsufficientPrivilege, RaiseException
from psycopg.sql import SQL, Identifier

from db import admin_auth, store
from db.migration_steps.v024_operational_rls import (
    POSTGRES_STATEMENTS as OPERATIONAL_SECURITY_SQL,
)
from db.migration_steps.v024_operational_rls import (
    TABLES as OPERATIONAL_TABLES,
)
from db.migrations import run_migrations
from scripts import migrate

pytestmark = pytest.mark.skipif(not os.environ.get("TEST_DATABASE_URL"), reason="requires isolated loopback PostgreSQL 17")


@pytest.fixture()
def pg_auth_schema(monkeypatch: pytest.MonkeyPatch):
    dsn = os.environ["TEST_DATABASE_URL"]
    with psycopg.connect(dsn, autocommit=True) as conn:
        row = conn.execute("SELECT version(), current_database()").fetchone()
        assert "PostgreSQL 17" in row[0] and row[1] == "xbray_test"
        assert urlparse(dsn).hostname in {"127.0.0.1", "localhost", "::1"}
        for role in ("anon", "authenticated"):
            conn.execute(f"DO $$ BEGIN CREATE ROLE {role} NOLOGIN; EXCEPTION WHEN duplicate_object THEN NULL; END $$")
        schema = f"admin_auth_{uuid4().hex}"
        conn.execute(f"CREATE SCHEMA {Identifier(schema).as_string(conn)}")
        conn.execute(f"GRANT USAGE ON SCHEMA {Identifier(schema).as_string(conn)} TO anon, authenticated")
    scoped = make_conninfo(dsn, options=f"-c search_path={schema}", application_name=f"auth_test_{schema}")
    monkeypatch.setattr(store, "DATABASE_URL", scoped)
    try:
        with psycopg.connect(scoped) as conn:
            run_migrations(conn, postgres=True)
            conn.commit()
            run_migrations(conn, postgres=True)
            conn.commit()
        yield schema, scoped
    finally:
        with psycopg.connect(dsn, autocommit=True) as conn:
            conn.execute(f"DROP SCHEMA {Identifier(schema).as_string(conn)} CASCADE")


def test_postgres_migration_is_idempotent_rls_enabled_and_browser_grants_revoked(pg_auth_schema):
    schema, dsn = pg_auth_schema
    with psycopg.connect(dsn) as conn:
        store.check_db_schema()
        assert migrate.main(["--check"]) == 0
        versions = conn.execute("SELECT count(*) FROM schema_migrations WHERE version = '022_admin_auth'").fetchone()[0]
        assert versions == 1
        for table in ("admin_memberships", "admin_sessions", "admin_oauth_flows", "admin_auth_events"):
            assert conn.execute(
                "SELECT rowsecurity FROM pg_tables WHERE schemaname = %s AND tablename = %s", (schema, table)
            ).fetchone()[0]
            assert not conn.execute(
                "SELECT has_table_privilege(%s, %s, 'SELECT')", ("anon", f"{schema}.{table}")
            ).fetchone()[0]
            assert not conn.execute(
                "SELECT has_table_privilege(%s, %s, 'SELECT')", ("authenticated", f"{schema}.{table}")
            ).fetchone()[0]
        for table in OPERATIONAL_TABLES:
            assert conn.execute(
                "SELECT rowsecurity FROM pg_tables WHERE schemaname = %s AND tablename = %s",
                (schema, table),
            ).fetchone()[0]
            for role in ("anon", "authenticated"):
                for privilege in ("SELECT", "INSERT", "UPDATE", "DELETE"):
                    assert not conn.execute(
                        "SELECT has_table_privilege(%s, %s, %s)",
                        (role, f"{schema}.{table}", privilege),
                    ).fetchone()[0]
            assert conn.execute(
                "SELECT has_table_privilege(current_user, %s, 'SELECT')",
                (f"{schema}.{table}",),
            ).fetchone()[0]
            for role in ("anon", "authenticated"):
                assert not conn.execute(
                    """SELECT EXISTS (
                        SELECT 1 FROM pg_attribute
                        WHERE attrelid = %s::regclass AND attnum > 0 AND NOT attisdropped
                          AND (has_column_privilege(%s, attrelid, attname, 'SELECT')
                            OR has_column_privilege(%s, attrelid, attname, 'INSERT')
                            OR has_column_privilege(%s, attrelid, attname, 'UPDATE')
                            OR has_column_privilege(%s, attrelid, attname, 'REFERENCES'))
                    )""",
                    (f"{schema}.{table}", role, role, role, role),
                ).fetchone()[0]
        conn.execute("SET ROLE anon")
        with pytest.raises(InsufficientPrivilege):
            conn.execute("SELECT * FROM admin_sessions").fetchall()
        conn.rollback()
        conn.execute("SET ROLE anon")
        with pytest.raises(InsufficientPrivilege):
            conn.execute("SELECT * FROM refresh_runs").fetchall()
        conn.rollback()
        conn.execute("CREATE TABLE future_browser_grant_probe (id integer)")
        for role in ("anon", "authenticated"):
            assert not conn.execute(
                "SELECT has_table_privilege(%s, %s, 'SELECT')",
                (role, f"{schema}.future_browser_grant_probe"),
            ).fetchone()[0]


def test_operational_migration_fails_closed_on_global_browser_default_acl(pg_auth_schema):
    _schema, dsn = pg_auth_schema
    with psycopg.connect(dsn) as conn:
        creator = conn.execute("SELECT current_user").fetchone()[0]
        conn.execute(
            SQL("ALTER DEFAULT PRIVILEGES FOR ROLE {} GRANT SELECT ON TABLES TO anon")
            .format(Identifier(creator))
        )
        with pytest.raises(RaiseException, match="global browser-role default ACL detected"):
            conn.execute(OPERATIONAL_SECURITY_SQL[0])
        conn.rollback()


def test_operational_migration_fails_closed_on_inherited_browser_table_grant(pg_auth_schema):
    schema, dsn = pg_auth_schema
    inherited_role = f"browser_parent_{uuid4().hex}"
    with psycopg.connect(dsn) as conn:
        conn.execute(SQL("CREATE ROLE {} NOLOGIN").format(Identifier(inherited_role)))
        conn.execute(
            SQL("GRANT SELECT ON {}.refresh_runs TO {}")
            .format(Identifier(schema), Identifier(inherited_role))
        )
        conn.execute(SQL("GRANT {} TO anon").format(Identifier(inherited_role)))
        with pytest.raises(RaiseException, match="effective browser-role table privilege remains"):
            conn.execute(OPERATIONAL_SECURITY_SQL[-2])
        conn.rollback()


def test_postgres_oauth_claim_is_atomic_across_independent_connections(pg_auth_schema):
    _schema, scoped = pg_auth_schema
    monkeypatch_store_url = store.DATABASE_URL
    assert monkeypatch_store_url == scoped
    flow_id, handle, state, binding = str(uuid4()), "h" * 64, "s" * 64, "b" * 64
    now = datetime.now(timezone.utc)
    admin_auth.create_flow(flow_id=flow_id, handle_hash=handle, state_hash=state,
                           binding_hash=binding, ciphertext=b"cipher", nonce=b"n" * 12,
                           key_version="test-v1", callback_url="https://app.invalid/callback",
                           expires_at=now + timedelta(minutes=10), now=now)

    def claim():
        return admin_auth.consume_flow(state_hash=state, handle_hash=handle, binding_hash=binding, now=now)

    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(lambda _item: claim(), range(2)))
    assert sum(result is not None for result in results) == 1


def test_postgres_membership_revoke_audits_and_revokes_all_sessions(pg_auth_schema):
    _schema, _scoped = pg_auth_schema
    user_id, actor = str(uuid4()), str(uuid4())
    now = datetime.now(timezone.utc)
    admin_auth.grant_admin(user_id, granted_by=actor, reason="operator-approved", now=now)
    assert admin_auth.is_admin(user_id)
    raw_hash = "a" * 64
    admin_auth.establish_session(session_id=str(uuid4()), token_hash=raw_hash, user_id=user_id,
                                 csrf_hash="c" * 64, idle_expires_at=now + timedelta(minutes=30),
                                 absolute_expires_at=now + timedelta(hours=8),
                                 previous_token_hash=None, now=now)
    assert admin_auth.get_session(raw_hash, now=now) is not None
    assert admin_auth.revoke_admin(user_id, granted_by=actor, reason="operator-revoked", now=now)
    assert not admin_auth.is_admin(user_id)
    assert admin_auth.get_session(raw_hash, now=now) is None


def test_postgres_activity_uses_database_clock_and_respects_absolute_limit(pg_auth_schema):
    _schema, _scoped = pg_auth_schema
    user_id = str(uuid4())
    admin_auth.grant_admin(user_id, granted_by=str(uuid4()), reason="operator-approved")
    now = datetime.now(timezone.utc)
    token_hash = "d" * 64
    admin_auth.establish_session(
        session_id=str(uuid4()), token_hash=token_hash, user_id=user_id,
        csrf_hash="e" * 64,
        idle_expires_at=now + timedelta(seconds=45),
        absolute_expires_at=now + timedelta(seconds=45),
        previous_token_hash=None,
        now=now - timedelta(seconds=61),
    )
    assert admin_auth.update_activity(token_hash, idle_seconds=1800)
    record = admin_auth.get_session(token_hash)
    assert record is not None
    assert record["idle_expires_at"] <= record["absolute_expires_at"]
