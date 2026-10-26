import sqlite3

from db.migration_steps.v016_candidate_acquisition import SQLITE_STATEMENTS


def test_candidate_acquisition_migration_is_additive_and_idempotent():
    connection = sqlite3.connect(":memory:")

    for statement in SQLITE_STATEMENTS:
        connection.execute(statement)
    for statement in SQLITE_STATEMENTS:
        connection.execute(statement)

    tables = {
        row[0]
        for row in connection.execute(
            "SELECT name FROM sqlite_master WHERE type = 'table'"
        )
    }
    assert {
        "source_acquisition_attempts",
        "source_observation_origins",
        "source_acquisition_commits",
    } <= tables
    assert connection.execute(
        "SELECT COUNT(*) FROM source_observation_origins"
    ).fetchone() == (0,)


def test_candidate_acquisition_schema_rejects_invalid_route_and_status():
    connection = sqlite3.connect(":memory:")
    for statement in SQLITE_STATEMENTS:
        connection.execute(statement)

    try:
        connection.execute(
            "INSERT INTO source_acquisition_attempts "
            "(attempt_id, extraction_id, source, indicator, route, status, started_at) "
            "VALUES ('a', 'e', 'bcb_sgs', '433', 'alternate', 'running', 'now')"
        )
    except sqlite3.IntegrityError:
        pass
    else:
        raise AssertionError("unknown acquisition route was accepted")
