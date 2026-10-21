from typing import Final

MIGRATION_NAME: Final = "012_import_validation_rows"
SQLITE_STATEMENTS: Final[tuple[str, ...]] = ()
POSTGRES_STATEMENTS: Final[tuple[str, ...]] = (
    "ALTER TABLE xbray_import_private.rows ADD COLUMN IF NOT EXISTS purpose TEXT NOT NULL DEFAULT 'insert'",
    "ALTER TABLE xbray_import_private.rows DROP CONSTRAINT IF EXISTS rows_purpose_check",
    "ALTER TABLE xbray_import_private.rows ADD CONSTRAINT rows_purpose_check CHECK (purpose IN ('insert','validation'))",
    "ALTER TABLE xbray_import_private.chunks ADD COLUMN IF NOT EXISTS purpose TEXT NOT NULL DEFAULT 'insert'",
    "ALTER TABLE xbray_import_private.chunks DROP CONSTRAINT IF EXISTS chunks_purpose_check",
    "ALTER TABLE xbray_import_private.chunks ADD CONSTRAINT chunks_purpose_check CHECK (purpose IN ('insert','validation'))",
    "REVOKE ALL ON ALL TABLES IN SCHEMA xbray_import_private FROM PUBLIC",
    """DO $roles$
    BEGIN
        IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'anon') THEN
            EXECUTE 'REVOKE ALL ON ALL TABLES IN SCHEMA xbray_import_private FROM anon';
        END IF;
        IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'authenticated') THEN
            EXECUTE 'REVOKE ALL ON ALL TABLES IN SCHEMA xbray_import_private FROM authenticated';
        END IF;
    END
    $roles$""",
)
