from typing import Final

from db.migration_steps import (
    v001_observability,
    v002_sidra_observations,
    v003_derived_snapshots,
    v004_macro_score_snapshots,
    v005_resumable_refresh,
    v006_retrospective_macro_history,
    v007_retrospective_di,
    v008_di_maturity_provenance,
    v009_b3_instrument_snapshots,
    v010_production_data_imports,
    v011_private_import_staging,
    v012_import_validation_rows,
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
    (
        v004_macro_score_snapshots.MIGRATION_NAME,
        v004_macro_score_snapshots.SQLITE_STATEMENTS,
        v004_macro_score_snapshots.POSTGRES_STATEMENTS,
    ),
    (
        v005_resumable_refresh.MIGRATION_NAME,
        v005_resumable_refresh.SQLITE_STATEMENTS,
        v005_resumable_refresh.POSTGRES_STATEMENTS,
    ),
    (
        v006_retrospective_macro_history.MIGRATION_NAME,
        v006_retrospective_macro_history.SQLITE_STATEMENTS,
        v006_retrospective_macro_history.POSTGRES_STATEMENTS,
    ),
    (
        v007_retrospective_di.MIGRATION_NAME,
        v007_retrospective_di.SQLITE_STATEMENTS,
        v007_retrospective_di.POSTGRES_STATEMENTS,
    ),
    (
        v008_di_maturity_provenance.MIGRATION_NAME,
        v008_di_maturity_provenance.SQLITE_STATEMENTS,
        v008_di_maturity_provenance.POSTGRES_STATEMENTS,
    ),
    (
        v009_b3_instrument_snapshots.MIGRATION_NAME,
        v009_b3_instrument_snapshots.SQLITE_STATEMENTS,
        v009_b3_instrument_snapshots.POSTGRES_STATEMENTS,
    ),
    (
        v010_production_data_imports.MIGRATION_NAME,
        v010_production_data_imports.SQLITE_STATEMENTS,
        v010_production_data_imports.POSTGRES_STATEMENTS,
    ),
    (
        v011_private_import_staging.MIGRATION_NAME,
        v011_private_import_staging.SQLITE_STATEMENTS,
        v011_private_import_staging.POSTGRES_STATEMENTS,
    ),
    (
        v012_import_validation_rows.MIGRATION_NAME,
        v012_import_validation_rows.SQLITE_STATEMENTS,
        v012_import_validation_rows.POSTGRES_STATEMENTS,
    ),
)

__all__ = ["MIGRATIONS"]
