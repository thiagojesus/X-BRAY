from typing import Final

MIGRATION_NAME: Final = "011_private_import_staging"
SQLITE_STATEMENTS: Final[tuple[str, ...]] = ()
POSTGRES_STATEMENTS: Final[tuple[str, ...]] = (
    "CREATE SCHEMA IF NOT EXISTS xbray_import_private",
    "REVOKE ALL ON SCHEMA xbray_import_private FROM PUBLIC",
    """DO $roles$
    BEGIN
        IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'anon') THEN
            EXECUTE 'REVOKE ALL ON SCHEMA xbray_import_private FROM anon';
        END IF;
        IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'authenticated') THEN
            EXECUTE 'REVOKE ALL ON SCHEMA xbray_import_private FROM authenticated';
        END IF;
    END
    $roles$""",
    """CREATE TABLE IF NOT EXISTS xbray_import_private.batches (
        batch_id TEXT PRIMARY KEY,
        target_ref TEXT NOT NULL,
        transport_version SMALLINT NOT NULL CHECK (transport_version = 1),
        manifest_json TEXT NOT NULL,
        baseline_json JSONB NOT NULL,
        row_counts_json JSONB NOT NULL,
        state TEXT NOT NULL CHECK (state IN ('staging', 'ready')),
        created_at TIMESTAMPTZ NOT NULL DEFAULT now()
    )""",
    """CREATE TABLE IF NOT EXISTS xbray_import_private.rows (
        batch_id TEXT NOT NULL REFERENCES xbray_import_private.batches(batch_id) ON DELETE CASCADE,
        table_name TEXT NOT NULL CHECK (table_name IN (
            'retrospective_di_observations', 'b3_di_instrument_snapshots',
            'retrospective_di_price_identities', 'retrospective_macro_values',
            'observation_vintages'
        )),
        chunk_index INTEGER NOT NULL CHECK (chunk_index >= 0),
        ordinal INTEGER NOT NULL CHECK (ordinal >= 0),
        payload_json JSONB NOT NULL,
        payload_md5 TEXT NOT NULL CHECK (payload_md5 ~ '^[0-9a-f]{32}$'),
        row_identity_json TEXT NOT NULL,
        row_sha256 TEXT NOT NULL CHECK (row_sha256 ~ '^[0-9a-f]{64}$'),
        PRIMARY KEY (batch_id, table_name, ordinal)
    )""",
    """CREATE TABLE IF NOT EXISTS xbray_import_private.chunks (
        batch_id TEXT NOT NULL REFERENCES xbray_import_private.batches(batch_id) ON DELETE CASCADE,
        table_name TEXT NOT NULL,
        chunk_index INTEGER NOT NULL CHECK (chunk_index >= 0),
        row_count INTEGER NOT NULL CHECK (row_count > 0),
        chunk_sha256 TEXT NOT NULL CHECK (chunk_sha256 ~ '^[0-9a-f]{64}$'),
        PRIMARY KEY (batch_id, table_name, chunk_index)
    )""",
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
