from typing import Final

MIGRATION_NAME: Final = "008_di_maturity_provenance"

SQLITE_STATEMENTS: Final[tuple[str, ...]] = (
    "ALTER TABLE retrospective_di_observations ADD COLUMN maturity_basis TEXT NOT NULL DEFAULT 'legacy_local_unverified'",
)

POSTGRES_STATEMENTS: Final[tuple[str, ...]] = (
    "ALTER TABLE retrospective_di_observations ADD COLUMN IF NOT EXISTS maturity_basis TEXT NOT NULL DEFAULT 'legacy_local_unverified'",
)
