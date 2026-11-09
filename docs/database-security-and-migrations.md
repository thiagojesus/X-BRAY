# Database security and explicit migrations

## Contract

The API process performs a read-only migration-journal compatibility check at
startup. It never creates the journal, runs migrations, or repairs schema,
regardless of deployment environment flags. Importing the app and handling
requests do not authorize DDL. Run the migration command as a separate,
reviewed operator action from `backend/`:

```sh
python scripts/migrate.py --plan       # read-only; default behavior
python scripts/migrate.py --check      # read-only compatibility gate
python scripts/migrate.py --apply      # explicit DDL action
python scripts/migrate.py --check      # verify after application
```

The command prints only the SQLite label or a public Supabase project
reference; connection credentials are not arguments and are not printed.
`--apply` is not part of deployment startup and is not performed by CI or this
change. Review the plan and target before authorizing it. Migration application
does not deploy application code, and deployment does not apply migrations.

## Browser roles and managed objects

Migration `024_operational_rls` is additive; migrations `001`–`023` are
immutable. It enables RLS without adding policies on the 25 listed managed
operational tables, revokes table and column privileges from `PUBLIC`,
`anon`, and `authenticated`, and revokes privileges on sequences owned by
those tables. It scopes default-privilege revocation to the migration's
effective PostgreSQL role in the current app schema (`public` in the hosted
application). Before changing the schema it fails closed if that creator has
global default grants to `PUBLIC`, `anon`, or `authenticated` for tables or
sequences: PostgreSQL schema-specific default revokes cannot cancel global
defaults. Resolve this only with a dedicated migration creator role or a
separately approved ACL correction; this migration intentionally does not
change global defaults across schemas. It also aborts if role inheritance
leaves any effective browser table, column, or owned-sequence privilege after
the scoped revokes. This preserves the trusted database
role's existing access; it does not validate the production runtime identity
or eliminate table-owner/superuser RLS bypass. Confirm the migration creator
role and runtime role as a separate release gate.

No authorization is added to the four private admin-auth tables. Their RLS
and deny-by-default posture remain unchanged. No production migration,
deployment, role change, or browser access probe is authorized by this
implementation. Direct browser database access is not an application data
path; the browser uses the FastAPI API.

## Deployment sequence and compatibility

The code-only release gate must run `python scripts/migrate.py --check`
against the actual target before deploying. It fails if the journal is
missing, a registered migration is pending, or an unrecognized version is
installed. A schema migration is a separately reviewed operator step; after
it, deploy the compatible application build. Keep the prior API deployment
available until the new build's read-only startup check and health endpoint
pass. The previous release does not know about 024 and must not be treated as
the schema verifier. Do not roll back the database migration; application
rollback is permitted only after confirming the old code remains compatible
with the additive change. Production role identity and effective grants must
be verified independently before rollout. No production state was changed by
this PR.

## Local verification

Use a disposable PostgreSQL 17 database to verify effective privileges with
`SET ROLE anon` and `SET ROLE authenticated` for SELECT/INSERT/UPDATE/DELETE,
and separately verify trusted backend and migration-role operations. Verify
new tables created by the actual migration role do not receive browser grants.
Do not use production data or credentials for these tests.
