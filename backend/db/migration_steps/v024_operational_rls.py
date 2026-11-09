"""Deny direct PostgREST access to app-managed operational tables."""

from typing import Final

MIGRATION_NAME: Final = "024_operational_rls"
TABLES: Final[tuple[str, ...]] = (
    "b3_di_instrument_snapshots", "focus_forecasts",
    "production_data_import_batch_rows", "production_data_import_batches",
    "refresh_attempt_lifecycle", "refresh_attempt_milestones",
    "refresh_attempt_reconciliations", "refresh_runs", "refresh_work_items",
    "retrospective_di_observations", "retrospective_di_price_identities",
    "retrospective_macro_values", "sgs_archive_cursors", "sgs_coverage_receipts",
    "sgs_refresh_plan_units", "sgs_refresh_plans", "sgs_unit_commits",
    "source_access_state", "source_acquisition_attempts",
    "source_acquisition_commits", "source_canary_bindings", "source_extractions",
    "source_observation_origins", "tr_period_observations", "treasury_cohort_commits",
)

_NAMES = ", ".join(repr(name) for name in TABLES)
SQLITE_STATEMENTS: Final[tuple[str, ...]] = ()
POSTGRES_STATEMENTS: Final[tuple[str, ...]] = (
    """DO $$ DECLARE creator_oid oid := (SELECT oid FROM pg_roles WHERE rolname = current_user);
    BEGIN
        IF EXISTS (
            SELECT 1 FROM pg_default_acl defaults
            CROSS JOIN LATERAL aclexplode(defaults.defaclacl) privileges
            WHERE defaults.defaclrole = creator_oid
              AND defaults.defaclnamespace = 0
              AND defaults.defaclobjtype IN ('r', 'S')
              AND privileges.grantee IN (
                  0,
                  (SELECT oid FROM pg_roles WHERE rolname = 'anon'),
                  (SELECT oid FROM pg_roles WHERE rolname = 'authenticated')
              )
              AND (
                  (defaults.defaclobjtype = 'r' AND privileges.privilege_type IN
                      ('SELECT', 'INSERT', 'UPDATE', 'DELETE', 'TRUNCATE', 'REFERENCES', 'TRIGGER'))
                  OR (defaults.defaclobjtype = 'S' AND privileges.privilege_type IN
                      ('USAGE', 'SELECT', 'UPDATE'))
              )
        ) THEN
            RAISE EXCEPTION 'global browser-role default ACL detected for migration creator; use an approved dedicated creator role or separately approved ACL correction';
        END IF;
    END $$""",
    f"""DO $$ DECLARE app_schema text := current_schema(); table_name text;
    BEGIN FOREACH table_name IN ARRAY ARRAY[{_NAMES}] LOOP
        EXECUTE 'ALTER TABLE ' || quote_ident(app_schema) || '.' || quote_ident(table_name)
            || ' ENABLE ROW LEVEL SECURITY';
    END LOOP; END $$""",
    f"""DO $$ DECLARE app_schema text := current_schema(); role_name text;
    table_name text; sequence_name text; column_name text; privilege_name text; BEGIN
        FOREACH role_name IN ARRAY ARRAY['PUBLIC', 'anon', 'authenticated'] LOOP
            IF role_name <> 'PUBLIC' AND NOT EXISTS
                (SELECT 1 FROM pg_roles WHERE rolname = role_name) THEN CONTINUE; END IF;
            FOREACH table_name IN ARRAY ARRAY[{_NAMES}] LOOP
                EXECUTE 'REVOKE ALL PRIVILEGES ON TABLE ' || quote_ident(app_schema) || '.'
                    || quote_ident(table_name) || ' FROM ' ||
                    CASE WHEN role_name = 'PUBLIC' THEN 'PUBLIC' ELSE quote_ident(role_name) END;
                FOR column_name IN
                    SELECT attname FROM pg_attribute
                    WHERE attrelid = (quote_ident(app_schema) || '.' || quote_ident(table_name))::regclass
                      AND attnum > 0 AND NOT attisdropped
                LOOP
                    FOREACH privilege_name IN ARRAY ARRAY['SELECT', 'INSERT', 'UPDATE', 'REFERENCES'] LOOP
                        EXECUTE 'REVOKE ' || privilege_name || ' (' || quote_ident(column_name)
                            || ') ON TABLE ' || quote_ident(app_schema) || '.' || quote_ident(table_name)
                            || ' FROM ' || CASE WHEN role_name = 'PUBLIC' THEN 'PUBLIC' ELSE quote_ident(role_name) END;
                    END LOOP;
                END LOOP;
                FOR sequence_name IN
                    SELECT DISTINCT seq.relname FROM pg_class seq
                    JOIN pg_depend dep ON dep.objid = seq.oid
                    JOIN pg_class tbl ON tbl.oid = dep.refobjid
                    JOIN pg_namespace ns ON ns.oid = tbl.relnamespace
                    WHERE seq.relkind = 'S' AND ns.nspname = app_schema
                      AND tbl.relname = table_name
                LOOP
                    EXECUTE 'REVOKE ALL PRIVILEGES ON SEQUENCE ' || quote_ident(app_schema) || '.'
                        || quote_ident(sequence_name) || ' FROM ' ||
                        CASE WHEN role_name = 'PUBLIC' THEN 'PUBLIC' ELSE quote_ident(role_name) END;
                END LOOP;
            END LOOP;
        END LOOP;
    END $$""",
    f"""DO $$ DECLARE app_schema text := current_schema(); role_name text;
    table_name text; column_name text; sequence_name text; privilege_name text;
    object_oid oid; BEGIN
        FOREACH role_name IN ARRAY ARRAY['anon', 'authenticated'] LOOP
            IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = role_name) THEN CONTINUE; END IF;
            FOREACH table_name IN ARRAY ARRAY[{_NAMES}] LOOP
                object_oid := (quote_ident(app_schema) || '.' || quote_ident(table_name))::regclass;
                FOREACH privilege_name IN ARRAY ARRAY['SELECT', 'INSERT', 'UPDATE', 'DELETE'] LOOP
                    IF has_table_privilege(role_name, object_oid, privilege_name) THEN
                        RAISE EXCEPTION 'effective browser-role table privilege remains on managed table';
                    END IF;
                END LOOP;
                FOR column_name IN SELECT attname FROM pg_attribute
                    WHERE attrelid = object_oid AND attnum > 0 AND NOT attisdropped
                LOOP
                    FOREACH privilege_name IN ARRAY ARRAY['SELECT', 'INSERT', 'UPDATE', 'REFERENCES'] LOOP
                        IF has_column_privilege(role_name, object_oid, column_name, privilege_name) THEN
                            RAISE EXCEPTION 'effective browser-role column privilege remains on managed table';
                        END IF;
                    END LOOP;
                END LOOP;
                FOR sequence_name IN
                    SELECT DISTINCT seq.relname FROM pg_class seq
                    JOIN pg_depend dep ON dep.objid = seq.oid
                    JOIN pg_class tbl ON tbl.oid = dep.refobjid
                    JOIN pg_namespace ns ON ns.oid = tbl.relnamespace
                    WHERE seq.relkind = 'S' AND ns.nspname = app_schema
                      AND tbl.relname = table_name
                LOOP
                    object_oid := (quote_ident(app_schema) || '.' || quote_ident(sequence_name))::regclass;
                    FOREACH privilege_name IN ARRAY ARRAY['USAGE', 'SELECT', 'UPDATE'] LOOP
                        IF has_sequence_privilege(role_name, object_oid, privilege_name) THEN
                            RAISE EXCEPTION 'effective browser-role sequence privilege remains on managed table';
                        END IF;
                    END LOOP;
                END LOOP;
            END LOOP;
        END LOOP;
    END $$""",
    """DO $$ DECLARE app_schema text := current_schema(); role_name text;
    BEGIN FOREACH role_name IN ARRAY ARRAY['anon', 'authenticated'] LOOP
        IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = role_name) THEN
            EXECUTE 'ALTER DEFAULT PRIVILEGES FOR ROLE ' || quote_ident(current_user)
                || ' IN SCHEMA ' || quote_ident(app_schema) || ' REVOKE ALL ON TABLES FROM '
                || quote_ident(role_name);
            EXECUTE 'ALTER DEFAULT PRIVILEGES FOR ROLE ' || quote_ident(current_user)
                || ' IN SCHEMA ' || quote_ident(app_schema) || ' REVOKE ALL ON SEQUENCES FROM '
                || quote_ident(role_name);
        END IF;
    END LOOP;
    EXECUTE 'ALTER DEFAULT PRIVILEGES FOR ROLE ' || quote_ident(current_user)
        || ' IN SCHEMA ' || quote_ident(app_schema) || ' REVOKE ALL ON TABLES FROM PUBLIC';
    EXECUTE 'ALTER DEFAULT PRIVILEGES FOR ROLE ' || quote_ident(current_user)
        || ' IN SCHEMA ' || quote_ident(app_schema) || ' REVOKE ALL ON SEQUENCES FROM PUBLIC';
    END $$""",
)
