from typing import Final

MIGRATION_NAME: Final = "003_derived_snapshots"

SQLITE_STATEMENTS: Final[tuple[str, ...]] = (
    """CREATE TABLE IF NOT EXISTS derived_snapshots (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        key TEXT NOT NULL,
        as_of TEXT NOT NULL,
        methodology_version TEXT NOT NULL,
        inputs_hash TEXT NOT NULL,
        payload_json TEXT NOT NULL,
        calculation_cycle_id TEXT NOT NULL,
        origin TEXT NOT NULL CHECK (origin IN ('production_refresh', 'test_fixture')),
        created_at TEXT NOT NULL,
        UNIQUE (key, as_of, methodology_version, inputs_hash)
    )""",
    """CREATE INDEX IF NOT EXISTS derived_snapshots_latest_idx
    ON derived_snapshots (key, as_of DESC, created_at DESC, id DESC)""",
)

POSTGRES_STATEMENTS: Final[tuple[str, ...]] = (
    SQLITE_STATEMENTS[0].replace("id INTEGER PRIMARY KEY AUTOINCREMENT", "id BIGSERIAL PRIMARY KEY"),
    SQLITE_STATEMENTS[1],
)
