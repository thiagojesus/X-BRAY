from typing import Final

from db.migration_steps import (
    v001_observability,
    v002_sidra_observations,
    v003_derived_snapshots,
)

Migration = tuple[str, tuple[str, ...], tuple[str, ...]]

MIGRATIONS: Final[tuple[Migration, ...]] = (
    (
        v001_observability.MIGRATION_NAME,
        v001_observability.SQLITE_STATEMENTS,
        v001_observability.POSTGRES_STATEMENTS,
    ),
    (
        v002_sidra_observations.MIGRATION_NAME,
        v002_sidra_observations.SQLITE_STATEMENTS,
        v002_sidra_observations.POSTGRES_STATEMENTS,
    ),
    (
        v003_derived_snapshots.MIGRATION_NAME,
        v003_derived_snapshots.SQLITE_STATEMENTS,
        v003_derived_snapshots.POSTGRES_STATEMENTS,
    ),
)

__all__ = ["MIGRATIONS"]
