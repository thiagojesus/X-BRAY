from typing import Final

MIGRATION_NAME: Final = "009_b3_instrument_snapshots"

SQLITE_STATEMENTS: Final[tuple[str, ...]] = (
    "ALTER TABLE retrospective_di_observations ADD COLUMN security_id TEXT",
    (
        "CREATE TABLE IF NOT EXISTS b3_di_instrument_snapshots ("
        "snapshot_date TEXT NOT NULL, report_date TEXT NOT NULL, symbol TEXT NOT NULL, "
        "security_id TEXT NOT NULL, fin_instrm_id TEXT NOT NULL, maturity TEXT NOT NULL, trading_start TEXT, trading_end TEXT, "
        "source_hash TEXT NOT NULL, source_name TEXT NOT NULL, message_definition TEXT NOT NULL, "
        "message_id TEXT NOT NULL, captured_at TEXT NOT NULL, available_at TEXT NOT NULL, "
        "source_basis TEXT NOT NULL CHECK(source_basis='b3_instrument_snapshot_v1'), "
        "PRIMARY KEY (source_hash, symbol, security_id, maturity))"
    ),
    (
        "CREATE INDEX IF NOT EXISTS b3_di_instrument_lookup_idx "
        "ON b3_di_instrument_snapshots (symbol, security_id, report_date)"
    ),
    (
        "CREATE TABLE IF NOT EXISTS retrospective_di_price_identities ("
        "trade_date TEXT NOT NULL, symbol TEXT NOT NULL, source_hash TEXT NOT NULL, "
        "source_member TEXT NOT NULL, security_id TEXT NOT NULL, captured_at TEXT NOT NULL, "
        "source_basis TEXT NOT NULL CHECK(source_basis='b3_price_identity_v1'), "
        "PRIMARY KEY (trade_date,symbol,source_hash,source_member))"
    ),
)

POSTGRES_STATEMENTS: Final[tuple[str, ...]] = (
    "ALTER TABLE retrospective_di_observations ADD COLUMN IF NOT EXISTS security_id TEXT",
    SQLITE_STATEMENTS[1],
    SQLITE_STATEMENTS[2],
    SQLITE_STATEMENTS[3],
)
