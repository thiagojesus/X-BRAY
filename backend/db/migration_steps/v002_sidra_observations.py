from typing import Final

MIGRATION_NAME: Final = "002_sidra_observations"

SQLITE_STATEMENTS: Final[tuple[str, ...]] = (
    """CREATE TABLE IF NOT EXISTS sidra_observations (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        dataset_key TEXT NOT NULL,
        table_id INTEGER NOT NULL,
        variable_id INTEGER NOT NULL,
        classification_id INTEGER NOT NULL DEFAULT 0,
        category_id INTEGER NOT NULL DEFAULT 0,
        territory_level TEXT NOT NULL,
        territory_code TEXT NOT NULL,
        reference_period TEXT NOT NULL,
        reference_label TEXT NOT NULL,
        value REAL NOT NULL,
        unit TEXT NOT NULL,
        base_version TEXT,
        source_published_at TEXT,
        first_seen_at TEXT NOT NULL,
        available_at TEXT NOT NULL,
        payload_hash TEXT NOT NULL,
        UNIQUE (
            dataset_key,
            table_id,
            variable_id,
            classification_id,
            category_id,
            territory_level,
            territory_code,
            reference_period,
            payload_hash
        )
    )""",
    """CREATE INDEX IF NOT EXISTS sidra_observations_current_idx
    ON sidra_observations (
        dataset_key,
        reference_period,
        available_at,
        first_seen_at,
        payload_hash
    )""",
)

POSTGRES_STATEMENTS: Final[tuple[str, ...]] = (
    SQLITE_STATEMENTS[0]
    .replace("id INTEGER PRIMARY KEY AUTOINCREMENT", "id BIGSERIAL PRIMARY KEY")
    .replace("value REAL NOT NULL", "value DOUBLE PRECISION NOT NULL"),
    SQLITE_STATEMENTS[1],
)
