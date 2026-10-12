from typing import Final

MIGRATION_NAME: Final = "004_macro_score_snapshots"

SQLITE_STATEMENTS: Final[tuple[str, ...]] = (
    """CREATE TABLE IF NOT EXISTS macro_score_snapshots (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        score_month TEXT NOT NULL,
        methodology_version TEXT NOT NULL,
        as_of TEXT NOT NULL,
        calculated_at TEXT NOT NULL,
        inputs_hash TEXT NOT NULL,
        payload_json TEXT NOT NULL,
        calculation_cycle_id TEXT NOT NULL,
        origin TEXT NOT NULL CHECK (origin IN ('production_refresh', 'test_fixture')),
        created_at TEXT NOT NULL,
        UNIQUE (score_month, methodology_version, inputs_hash)
    )""",
    """CREATE INDEX IF NOT EXISTS macro_score_snapshots_latest_idx
    ON macro_score_snapshots (score_month DESC, calculated_at DESC, inputs_hash DESC, id DESC)""",
)

POSTGRES_STATEMENTS: Final[tuple[str, ...]] = (
    SQLITE_STATEMENTS[0].replace("id INTEGER PRIMARY KEY AUTOINCREMENT", "id BIGSERIAL PRIMARY KEY"),
    SQLITE_STATEMENTS[1],
)
