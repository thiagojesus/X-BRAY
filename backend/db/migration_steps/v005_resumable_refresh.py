from typing import Final

MIGRATION_NAME: Final = "005_resumable_refresh"

SQLITE_STATEMENTS: Final[tuple[str, ...]] = (
    """CREATE TABLE IF NOT EXISTS refresh_runs (
        run_id TEXT PRIMARY KEY,
        cycle_id TEXT NOT NULL,
        state TEXT NOT NULL CHECK (state IN ('running', 'succeeded', 'partial', 'failed')),
        force INTEGER NOT NULL CHECK (force IN (0, 1)),
        manifest_version TEXT NOT NULL,
        derived_cutoff TEXT,
        macro_cutoff TEXT,
        created_at TEXT NOT NULL,
        updated_at TEXT NOT NULL,
        finished_at TEXT,
        final_result TEXT
    )""",
    """CREATE TABLE IF NOT EXISTS refresh_work_items (
        run_id TEXT NOT NULL,
        item_key TEXT NOT NULL,
        task TEXT NOT NULL,
        task_order INTEGER NOT NULL,
        status TEXT NOT NULL CHECK (status IN ('pending', 'claimed', 'succeeded', 'partial', 'failed', 'skipped')),
        attempt INTEGER NOT NULL DEFAULT 0 CHECK (attempt >= 0),
        claim_token TEXT,
        claim_expires_at TEXT,
        claimed_at TEXT,
        created_at TEXT NOT NULL,
        updated_at TEXT NOT NULL,
        finished_at TEXT,
        series_updated INTEGER NOT NULL DEFAULT 0 CHECK (series_updated >= 0),
        record_count INTEGER NOT NULL DEFAULT 0 CHECK (record_count >= 0),
        error_code TEXT,
        PRIMARY KEY (run_id, item_key),
        UNIQUE (run_id, task_order),
        FOREIGN KEY (run_id) REFERENCES refresh_runs (run_id),
        CHECK (
            (status = 'claimed' AND claim_token IS NOT NULL AND claim_expires_at IS NOT NULL)
            OR (
                status <> 'claimed'
                AND (
                    (claim_token IS NULL AND claim_expires_at IS NULL)
                    OR (claim_token IS NOT NULL AND claim_expires_at IS NOT NULL)
                )
            )
        )
    )""",
    """CREATE INDEX IF NOT EXISTS refresh_work_items_claim_idx
    ON refresh_work_items (run_id, status, task_order, claim_expires_at)""",
)

POSTGRES_STATEMENTS: Final[tuple[str, ...]] = SQLITE_STATEMENTS
