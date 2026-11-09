from __future__ import annotations

import sqlite3
import sys

import pytest

from db import store
from db.migration_steps import MIGRATIONS, v024_operational_rls
from scripts import admin_auth_membership, migrate


def test_runtime_schema_check_accepts_complete_journal() -> None:
    store.check_db_schema()


def test_migration_plan_is_read_only(capsys: pytest.CaptureFixture[str]) -> None:
    before = store._get_conn().execute(
        "SELECT version, applied_at FROM schema_migrations ORDER BY version"
    ).fetchall()
    assert migrate.main(["--plan"]) == 0
    assert "no database changes made" in capsys.readouterr().out
    after = store._get_conn().execute(
        "SELECT version, applied_at FROM schema_migrations ORDER BY version"
    ).fetchall()
    assert after == before


def test_migration_check_and_apply_are_noops_when_current(
    capsys: pytest.CaptureFixture[str],
) -> None:
    assert migrate.main(["--check"]) == 0
    assert "schema is compatible" in capsys.readouterr().out
    assert migrate.main(["--apply"]) == 0
    assert "applied and verified" in capsys.readouterr().out


def test_migration_check_rejects_pending_version_without_applying(
    capsys: pytest.CaptureFixture[str],
) -> None:
    connection = store._get_conn()
    connection.execute("DELETE FROM schema_migrations WHERE version = '024_operational_rls'")
    try:
        assert migrate.main(["--check"]) == 2
        assert "no database changes made" in capsys.readouterr().out
    finally:
        connection.execute(
            "INSERT INTO schema_migrations (version, applied_at) VALUES (?, ?)",
            ("024_operational_rls", "restored-for-test"),
        )


def test_explicit_apply_migrates_local_test_database(
    capsys: pytest.CaptureFixture[str],
) -> None:
    connection = store._get_conn()
    connection.execute("DELETE FROM schema_migrations WHERE version = '024_operational_rls'")
    assert migrate.main(["--apply"]) == 0
    assert "Schema migrations are applied and verified" in capsys.readouterr().out
    store.check_db_schema()


def test_target_label_redacts_database_credentials() -> None:
    label = migrate._target_label(
        "postgresql://private-user:private-password@db.abcdefghijklmnopqrst.supabase.co:5432/app"
    )
    assert "abcdefghijklmnopqrst" in label
    assert "private-user" not in label
    assert "private-password" not in label


def test_admin_membership_cli_checks_schema_before_operation(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    called: list[str] = []
    monkeypatch.setattr(sys, "argv", ["admin_auth_membership.py", "cleanup"])
    monkeypatch.setattr(admin_auth_membership, "check_db_schema", lambda: called.append("schema"))
    monkeypatch.setattr(
        admin_auth_membership.admin_auth,
        "cleanup",
        lambda batch_size: {"batch_size": batch_size},
    )
    assert admin_auth_membership.main() == 0
    assert called == ["schema"]


@pytest.mark.parametrize("command", ["grant", "revoke"])
def test_admin_membership_grant_and_revoke_require_schema_check(
    monkeypatch: pytest.MonkeyPatch, command: str,
) -> None:
    user_id = "00000000-0000-0000-0000-000000000001"
    actor_id = "00000000-0000-0000-0000-000000000002"
    reason = "operator-approved" if command == "grant" else "operator-revoked"
    monkeypatch.setattr(
        sys,
        "argv",
        ["admin_auth_membership.py", command, user_id, "--actor-id", actor_id, "--reason", reason],
    )
    checks: list[str] = []
    monkeypatch.setattr(admin_auth_membership, "check_db_schema", lambda: checks.append("checked"))
    monkeypatch.setattr(admin_auth_membership.admin_auth, "grant_admin", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(admin_auth_membership.admin_auth, "revoke_admin", lambda *_args, **_kwargs: True)
    assert admin_auth_membership.main() == 0
    assert checks == ["checked"]


def test_runtime_schema_check_fails_closed_without_modifying_schema(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(store, "DATABASE_URL", "")
    connection = sqlite3.connect(":memory:")
    connection.row_factory = sqlite3.Row
    monkeypatch.setattr(store, "_conn", connection)
    connection.execute("CREATE TABLE schema_migrations (version TEXT PRIMARY KEY)")
    connection.execute("INSERT INTO schema_migrations VALUES ('001_observability')")
    before = connection.execute(
        "SELECT name FROM sqlite_master WHERE type = 'table' ORDER BY name"
    ).fetchall()

    with pytest.raises(RuntimeError, match="pending migrations"):
        store.check_db_schema()

    after = connection.execute(
        "SELECT name FROM sqlite_master WHERE type = 'table' ORDER BY name"
    ).fetchall()
    assert after == before


def test_runtime_schema_check_does_not_create_missing_database_file(
    monkeypatch: pytest.MonkeyPatch, tmp_path,
) -> None:
    missing_db = tmp_path / "missing.db"
    monkeypatch.setattr(store, "DATABASE_URL", "")
    monkeypatch.setattr(store, "DB_PATH", missing_db)
    monkeypatch.setattr(store, "_conn", None)
    with pytest.raises(RuntimeError, match="database file and migration journal are missing"):
        store.check_db_schema()
    assert not missing_db.exists()


def test_read_only_migration_plan_does_not_create_missing_database(
    monkeypatch: pytest.MonkeyPatch, tmp_path,
) -> None:
    missing_db = tmp_path / "missing-plan.db"
    monkeypatch.setattr(store, "DATABASE_URL", "")
    monkeypatch.setattr(store, "DB_PATH", missing_db)
    monkeypatch.setattr(store, "_conn", None)
    pending, unknown = migrate._status()
    assert pending == sorted(name for name, _sqlite, _postgres in MIGRATIONS)
    assert unknown == []
    assert not missing_db.exists()


def test_read_only_plan_handles_existing_database_without_journal(
    monkeypatch: pytest.MonkeyPatch, tmp_path,
) -> None:
    empty_db = tmp_path / "empty.db"
    sqlite3.connect(empty_db).close()
    monkeypatch.setattr(store, "DATABASE_URL", "")
    monkeypatch.setattr(store, "DB_PATH", empty_db)
    monkeypatch.setattr(store, "_conn", None)
    pending, unknown = migrate._status()
    assert pending == sorted(name for name, _sqlite, _postgres in MIGRATIONS)
    assert unknown == []


def test_migration_cli_reports_redacted_runtime_error(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str],
) -> None:
    def fail_status() -> tuple[list[str], list[str]]:
        raise RuntimeError("do not expose this detail")

    monkeypatch.setattr(migrate, "_status", fail_status)
    assert migrate.main(["--check"]) == 2
    output = capsys.readouterr().err
    assert "connection details redacted" in output
    assert "do not expose this detail" not in output


def test_migration_cli_fails_when_post_apply_check_still_finds_pending(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str],
) -> None:
    states = iter([(["024_operational_rls"], []), (["024_operational_rls"], [])])
    monkeypatch.setattr(migrate, "_status", lambda: next(states))
    monkeypatch.setattr(store, "init_db", lambda: None)
    assert migrate.main(["--apply"]) == 2
    assert "post-check failed" in capsys.readouterr().err


def test_migration_status_redacts_non_schema_database_errors(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def fail_read() -> set[str]:
        raise sqlite3.OperationalError("private connection detail")

    monkeypatch.setattr(migrate, "_installed_versions", fail_read)
    with pytest.raises(RuntimeError, match="connection details are redacted"):
        migrate._status()


def test_runtime_schema_check_rejects_unrecognized_journal_version() -> None:
    store._get_conn().execute(
        "INSERT INTO schema_migrations (version, applied_at) VALUES ('999_future', 'now')"
    )
    with pytest.raises(RuntimeError, match="unknown schema versions"):
        store.check_db_schema()
    assert migrate.main(["--check"]) == 2


def test_security_migration_is_last_and_scoped_to_verified_manifest() -> None:
    assert MIGRATIONS[-1][0] == "024_operational_rls"
    assert len(v024_operational_rls.TABLES) == 25
    assert len(set(v024_operational_rls.TABLES)) == 25
    assert v024_operational_rls.TABLES == (
        "b3_di_instrument_snapshots", "focus_forecasts",
        "production_data_import_batch_rows", "production_data_import_batches",
        "refresh_attempt_lifecycle", "refresh_attempt_milestones",
        "refresh_attempt_reconciliations", "refresh_runs", "refresh_work_items",
        "retrospective_di_observations", "retrospective_di_price_identities",
        "retrospective_macro_values", "sgs_archive_cursors", "sgs_coverage_receipts",
        "sgs_refresh_plan_units", "sgs_refresh_plans", "sgs_unit_commits",
        "source_access_state", "source_acquisition_attempts",
        "source_acquisition_commits", "source_canary_bindings", "source_extractions",
        "source_observation_origins", "tr_period_observations", "treasury_cohort_commits",
    )
    sql = "\n".join(v024_operational_rls.POSTGRES_STATEMENTS).lower()
    assert "enable row level security" in sql
    assert "from public" in sql
    assert "quote_ident(role_name)" in sql
    assert "all sequences in schema public" not in sql
    assert "create policy" not in sql
    assert v024_operational_rls.SQLITE_STATEMENTS == ()


def test_migration_preserves_earlier_migration_identity() -> None:
    assert [entry[0] for entry in MIGRATIONS[:23]][-2:] == [
        "022_admin_auth",
        "023_admin_read_indexes",
    ]
