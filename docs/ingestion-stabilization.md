# Ingestion stabilization: current state and staged plan

## Phase 1 — contracts and characterization (implemented)

`backend/source_contracts.py` is a pure, immutable inventory (`acquisition-contract-v1`):
64 current SGS indicators, TR (226) full start/end identity, five Focus annual
families plus its distinct rolling IPCA 12-month contract, five SIDRA datasets,
the complete daily B3 DI curve, the ANBIMA IMA-B workbook cohort, and the
Tesouro offer-date/title/side cohort. It defines atomic identity and value
dimensions; it does not fetch, persist, change health/API status, or declare an
alternative equivalent. An absent official publication timestamp remains
unknown; capture time is not publication time. No contract or fallback is
defined for Polymarket or SGS 7811 as a substitute for TR.

Existing characterization remains in `tests/test_source_ingestion.py`,
`tests/test_source_ingestion_postgres.py`, `tests/test_sgs_tr_periods.py`,
`tests/test_source_audit.py`, `tests/test_refresh_run_execution.py`, and the
frontend refresh-client tests. The PG17 test covers the Focus producer, cache,
ledger, failure and partial-run path; the new contract tests bind its identities
to actual producer catalogs. Current audit parsers remain independent from
ingestion parsers. No public-network CI fixture or alternative-source claim is
introduced.

## Existing acquisition behavior and known risk (not changed in Phase 1)

These are code-policy settings, not guarantees of total job duration. In
particular, provider-level retries can be multiplied by surrounding collectors
or resumable task retries:

| Provider | Current behavior evidenced in code | Stabilization concern |
| --- | --- | --- |
| BCB SGS | Bounded HTTP helper has a one-attempt default; v4 annual 432/226 work-item fetches use a 20-second timeout, and the backend refresh client retries JSON requests up to three times. | The nested JSON/transport retry path must be given one shared per-work-item budget; do not assume the configured attempt counts multiply safely. |
| Focus | Annual family requests use a 30-second request timeout, 23-hour refresh age and separate family freshness keys; rolling IPCA 12m has a separate key. | Validate aggregate run limits and do not let one family watermark skip another. |
| SIDRA | 30-second timeout; up to three acquisition attempts with backoff. | Retries across dataset groups need a documented aggregate cap. |
| B3 DI | 30-second downloads; download retry count is two with 1.5-second delay; up to two download workers. Upsert occurs in the download/parser path. | A fallback must not run after partial writes; daily curve is the atomic unit and quota/cooldown must span callers. |
| ANBIMA | 120-second request timeout and binary cap inherited from `BINARY_MAX_BYTES` (100 MiB); workbook sheets are upserted during parsing. | Sequential sheet writes can leave a partial update; candidate selection needs acquire/validate-before-persist separation. |
| Tesouro | Refresh CSV downloader has a 90-second deadline, two attempts, up to 30 seconds retry wait, and 100 MiB binary cap; replacement is rejected when no rows persist. The independent audit probe is also bounded to 90 seconds. | Preserve whole cohort, title identity, both quote sides, and source-defined settlement semantics; do not treat the audit probe as a fallback. |

Automatic fallback is restricted to free, official sources with verified
equivalence of indicator definition, method, units, reference period, identity,
frequency and complete required fields. **No alternative is eligible today.**
SGS CSV/XML, SIDRA API v3, ANBIMA TXT and other documented routes are candidates
for future shadow evidence only; their equivalence has not been proven by these
contracts. Proxies (including Focus, ANBIMA indices, Selic or DI substitutes)
are not equivalent by resemblance. No production fallback is enabled.

## Phase 2 — failure taxonomy and bounded access policy (implemented)

The additive `015_ingestion_policy` migration adds `error_category` and
`retry_not_before` to extraction diagnostics and a provider-scope access-state
table. Existing extraction status values and source data tables are unchanged.
Transport, envelope, schema, persistence, freshness, logging, configuration,
deferred, and internal failures now have explicit classifications; unknown
programming failures are unhealthy validation errors, never inferred network
failures. The diagnostic API exposes the new fields additively.

The shared HTTP boundary charges every outbound request including approved
redirect hops against one context-local acquisition budget, caps each request
timeout to the remaining wall clock, and honors seconds/date `Retry-After` without
clipping it. Advice is parsed on every eligible response, including the final
attempt; if another request cannot fit, the bounded helper defers and records the
provider's UTC not-before time (preserving the longest advice observed).
Request/deadline defaults: SGS annual
window 3/20s, SGS general 3/220s, Focus per-indicator 3/45s (aggregate
`fetch_all_focus` 15/240s), SIDRA per-dataset 3/90s (aggregate refresh 15/240s),
B3 daily curve at most 64/240s (two requests for each statically planned file
date), ANBIMA workbook 3/120s, and Tesouro CSV cohort 2/90s. Budgets are
hard caps rather than throughput guarantees; long B3 backfills can defer and
resume on a subsequent run. Existing per-provider lower retry limits remain
in force.

Persistent circuit state is scoped to the primary endpoint per provider (not a
candidate/fallback). Three consecutive classified transient failures open it
for at least five minutes; a 429 opens immediately through its provider
not-before time. An atomic lease admits one half-open probe, restricted to one
outbound request; only a successful acquisition clears the breaker. Cache-only
reads do not touch the breaker. This phase does not add a candidate registry,
fallback selection, shadow data writes, or production operations.

## Phase 3 — candidate registry and shadow evidence (implemented, unqualified)

`backend/source_candidates.py` provides an immutable, exact-key catalog for all
six contracted source families. Only BCB SGS CSV and SIDRA aggregate API v3 have
strict shadow decoders. Focus, B3, ANBIMA TXT and Tesouro descriptors explicitly
remain disabled because an equivalent complete protocol is not yet verified.
All profiles have ingestion disabled and unverified licensing; even a successful
shadow comparison cannot make a profile production eligible.

`python -m shadow_cli shadow --candidate bcb_sgs:csv:433 --as-of YYYY-MM-DD
--artifact PATH` (or an exact catalog SIDRA id) issues a bounded, public,
allowlisted HTTPS comparison of the primary and candidate routes. The runner
does not call production refresh/persistence paths. Its local JSON artifact is
created exclusively with mode 0600 and includes checksums, profile/contract/code
versions, endpoint/parameter plan hashes, coverage, network counts and explicit
eligibility blockers. Existing artifact paths are not overwritten. A comparison
requires ten distinct dates, exact contract identities/cohorts, full units and
fields, matching latest dates, and finite matching values. Missing source
publication times stay unknown. HTTP or schema failure is an unsuccessful shadow
run, never a fallback signal. No real candidate is qualified by test fixtures.

No Phase 4 resolver, production candidate activation, scheduled job, database
candidate/evidence table, deployment, or production operation is included.

## Remaining ordered phases

1. **Phase 4 — resolver and atomic persistence:** separate acquire/validate from
   writes, select only eligible complete candidates on safe acquisition failure,
   and persist one coherent unit with explicit provenance and reconciliation.
2. **Phase 5 — health, audit and rollout controls:** additive optional outcome
   metadata, independent audit reporting, and staged operational rollout.

No phase after Phase 3 is implemented by this change. No scheduled job,
deployment, fallback activation, or production operation is part of this phase.
