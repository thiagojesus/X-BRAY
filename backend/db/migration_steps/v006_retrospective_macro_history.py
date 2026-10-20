from typing import Final

MIGRATION_NAME: Final = "006_retrospective_macro_history"

SQLITE_STATEMENTS: Final[tuple[str, ...]] = (
    """CREATE TABLE IF NOT EXISTS retrospective_macro_values (
        component_key TEXT NOT NULL,
        reference_month TEXT NOT NULL,
        value REAL,
        unit TEXT NOT NULL,
        formula_version TEXT NOT NULL,
        source_basis TEXT NOT NULL,
        fetched_at TEXT NOT NULL,
        inputs_json TEXT NOT NULL,
        inputs_hash TEXT NOT NULL,
        reason TEXT,
        PRIMARY KEY (component_key, reference_month, formula_version, inputs_hash)
    )""",
    "CREATE INDEX IF NOT EXISTS retrospective_macro_month_idx ON retrospective_macro_values (reference_month, component_key)",
)

POSTGRES_STATEMENTS: Final[tuple[str, ...]] = SQLITE_STATEMENTS
