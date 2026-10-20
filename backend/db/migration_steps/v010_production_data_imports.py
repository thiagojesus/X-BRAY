from typing import Final

MIGRATION_NAME: Final = "010_production_data_imports"

SQLITE_STATEMENTS: Final[tuple[str, ...]] = (
    """CREATE TABLE IF NOT EXISTS production_data_import_batches (
        batch_id TEXT PRIMARY KEY,
        source_manifest_json TEXT NOT NULL,
        imported_at TEXT NOT NULL,
        state TEXT NOT NULL
    )""",
    """CREATE TABLE IF NOT EXISTS production_data_import_batch_rows (
        batch_id TEXT NOT NULL,
        table_name TEXT NOT NULL,
        row_identity_json TEXT NOT NULL,
        row_sha256 TEXT NOT NULL,
        PRIMARY KEY (batch_id, table_name, row_identity_json)
    )""",
)

POSTGRES_STATEMENTS: Final[tuple[str, ...]] = SQLITE_STATEMENTS
