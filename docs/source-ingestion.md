# Source ingestion architecture

For the versioned acquisition-contract inventory, current provider-specific
retry/deadline risks, and staged stabilization plan, see
[`ingestion-stabilization.md`](ingestion-stabilization.md).
For refresh claim recovery, durable attempt outcomes, manifest v5 bounds, and
the evidence/deployment limitations of migrations 017/018, see
[`refresh-recovery-design.md`](refresh-recovery-design.md).

All supported provider refresh entrypoints use the adapter registry in
`backend/source_ingestion.py`. `SourceIngestionBase` records a logical attempt
around the provider-specific fetcher; concrete adapters identify BCB SGS,
Focus, SIDRA, B3 DI, ANBIMA, and Tesouro while the existing provider modules
retain their source-specific parsers, identity rules, and storage contracts.
GET/read helpers remain cache-only. Refresh endpoints and scheduled/resumable
tasks are the ingestion entrypoints.

Migration `013_source_extractions` adds a bounded, indexed operation ledger.
Each row starts as `running` and is finalized once. `network_called=0` and a
`cache_hit` status distinguish a cache read from a new extraction; cache hits
do not receive a fabricated `extracted_at`. `started_at`/`finished_at` are
operation timestamps; `source_data_date` is the latest recognizable valid
source date, not a download timestamp. Reference date bounds and qualifier JSON
preserve provider identity (for example Focus survey date versus target year,
or B3 trade date versus contract maturity). Counts whose meaning cannot be
established by the source adapter are `NULL`; they are not inferred to be
insert counts. Run, task, and cycle correlation is propagated through a
`ContextVar`, so concurrent tasks do not share mutable correlation state.

The source auditor remains an independent reader/prober. It does not import
the ingestion adapters, provider parsers, or ingestion caches. Its authenticated
read-only snapshot includes a bounded recent extraction history; the report
surfaces the latest attempt and latest network attempt separately for diagnosis.
Extraction records are diagnostic provenance and do not override its independent
comparison of official source values against stored observations.

Focus annual families have independent freshness metadata under
`focus_annual_last_refresh_<indicator>`. The old `focus_last_refresh` value is
written only for compatibility and is not used to decide whether another
family can skip its request. Each annual request uses the OData ordering
expression `Data desc` and validates returned indicator, survey date, target
year, base and finite median before storing data or advancing
freshness. Rolling 12-month IPCA continues using its separate key and contract.

B3 DI curve refreshes are recorded at the public `fetch_di_curves` boundary,
not at each daily ZIP download. A cache-only refresh therefore produces one
`cache_hit` row (`network_called=0`, no extraction timestamp); mixed cached and
downloaded days produce one network row with counts and reference dates from
the actual downloaded records. Existing reads and skipped/non-business days do
not trigger ledger writes or downloads.

SGS partial-window failures retain the underlying provider exception class in
`error_code`. For example, production refresh `40194928175` recorded series
432 and 226 as `RuntimeError`; the corresponding function logs establish that
both timed out (`ReadTimeout`) on the historical `01/01/2015`–`01/01/2025`
window. The newer-window requests succeeded. This identifies the actual
failed phase/cause without treating partial work as healthy or discarding
successful observations.

The ledger records attempt identity and safe error class, not raw payloads,
provider URLs, query strings, credentials, or licensed source files. Unknown
insert/update counts remain null. A source failure is not represented as a
successful extraction merely because older observations remain available to
cache-only readers.

## Conditional alternative-source producer flow (Phase 4 complete)

The real SGS window and SIDRA dataset producers now resolve an exact candidate
profile through `CandidateAcquisitionService` before entering the conditional
acquire/validate/select path. The default code-owned approval registry is empty;
catalog profiles remain disabled and rights-unverified, so production requests
continue using the legacy primary-only path. Test-only qualification is injected
through the internal service object, not request data or environment flags.

When a future reviewed profile is eligible, primary and candidate payloads are
acquired before any business write. Only classified transient primary acquisition
failures can select the alternate; schema/semantic, quota/cooldown, persistence,
and logging failures do not. The source operation id joins child attempt records.
The selected batch, per-row `source_observation_origins`, and
`source_acquisition_commits` receipt commit together with the SGS current/vintage
rows or SIDRA observations. A receipt reconciles an ambiguous commit response;
the implementation does not re-fetch another provider after a persistence
failure. Existing records are not retroactively assigned a candidate origin.

The transaction integration and actual producer routes have focused SQLite and
PostgreSQL 17 tests, and the full backend test and changed-file quality checks
pass. This does not represent candidate activation or deployment readiness.

## Phase 5 operator controls (inactive by default)

`SOURCE_FALLBACK_MODE` accepts `off`, `shadow`, `canary`, or `enabled`; an
absent or invalid value resolves to `off`. Acquisition mode is separate from
the code-owned candidate approval registry: changing the mode or supplying an
API/workflow field cannot establish parser proof, code review, or source reuse
rights. The current approval registry is empty, profiles remain disabled, and
rights evidence is not verified. Readiness reports these blockers explicitly.

The authenticated `GET /api/source-candidates/readiness` endpoint is a bounded,
read-only operator snapshot. It reads the fixed catalog and technical breaker
state only; it does not issue provider requests or claim a half-open probe
lease. `POST /api/source-candidates/canary` defaults to `dry_run=true`. In OFF
mode that is a read-only preflight. In SHADOW mode it may run the existing
bounded adapter using the exact catalog ID and an ephemeral artifact; it does
not write business data or freshness metadata. Write requests require CANARY
mode, an exact reviewed proof hash and contract version, explicit confirmation,
and a stable operation ID. For registered SGS and SIDRA adapters, the executor
binds a fixed catalog plan before HTTP, requires exact paired primary/candidate
agreement, and atomically stores the bounded unit with candidate provenance,
receipt, and replay binding. Replays verify the binding before HTTP. This
operator path does not update full-refresh freshness metadata. There is no API
to create approvals or edit profile URLs; the empty production approval
registry, disabled profiles, and unverified licensing keep writes unavailable
until separately reviewed.

The manual `source-canary.yml` workflow has no schedule, defaults to dry-run,
and is restricted to the main branch and existing API/token secrets. It does
not grant approval or bypass server-side gates. The weekly auditor validates
origin attestations against a code-owned, independent trust registry before any
candidate request; absent or unknown attestations are `unverified` and cause no
HTTP. Trusted SGS and SIDRA cohorts are fetched through the audit client's
bounded fixed-host probes and parsed independently of producer parsers/caches.
Candidate and primary findings are retained separately: alternate delivery never
clears a primary incident, while a later validated primary success can clear its
current degradation without removing historical fallback evidence. Audit reads
remain snapshot-only and do not touch ingestion, freshness, vintage, receipt, or
breaker state. The production trust registry remains empty while production
candidate approvals are empty.

## SGS annual refresh windows

The current resumable refresh manifest freezes annual SGS work items for series
432 and 226 from 2015-01-01 through the run's UTC creation date. Each year is a
separate claim/retry boundary, so an expired or failed run can resume without
re-downloading already completed years. The separate `sgs_juros_other` task
continues to refresh the remaining interest-rate series under their existing
request policy. The status endpoint aggregates the yearly work back to the four
logical SGS series and fails closed when a required yearly item is missing.

Successful annual window cache entries are bounded to seven days and are keyed
by canonical series code and inclusive ISO date bounds. An empty response is not
considered a complete window. Returned dates outside the requested interval and
conflicting duplicate dates fail validation; identical duplicate dates are
counted as received but stored once. No calendar-day interpolation is performed.

## Independent B3 and Tesouro audit probes

The weekly audit's fixed `/api/source-audit/probe` endpoint accepts only `b3` or
`tesouro` and a bounded UTC cutoff. It uses independent audit parsers and makes
bounded public GETs without ingestion adapters, cache fallback, or database
writes. It requires the existing refresh bearer token. GitHub Actions retains
each environment's result separately; a valid result from one environment can
verify a database comparison while a transport failure in the other is shown as
a warning, not as proof that both paths are healthy. Conflicting valid
environment observations are reported as disagreement and are never resolved
by selecting whichever happens to match the database. B3 application probes
have a 30-second in-process cooldown; the workflow serializes its GitHub and
application B3 calls with at least 30 seconds between them.

For SGS 432, source observations after the audit's captured UTC date are
excluded from the comparison. A valid observation on or before that date can
still produce a normal match/lag result, with future observations retained as a
non-blocking quality warning. A response containing only future observations
remains inconclusive.

Tesouro dual-probe coverage is checked per environment before either result can
be accepted for database comparison. The expected set is derived from the
snapshot's latest Treasury quote vintage at or before its capture cutoff,
using the canonical `bond_code` + title type + parsed maturity identity. Bonds
whose maturity is before that vintage's quote date and identities found only in
older quote vintages are excluded. If an otherwise parseable source response
omits a latest-vintage, unexpired database identity, that environment is marked
`missing_active_identity` and its missing identities are reported; this is a
coverage failure, not a claim that the provider truncated the file, and is not
silently treated as a database-missing instrument. A complete peer environment
does not erase that coverage failure. Only a transport failure in the other
environment permits a complete probe to verify with a transport warning. This
conservative rule does not infer official delistings; a future official
withdrawal contract would be needed to remove an identity from the expected set.
The latest vintage date is selected before maturity filtering, so a newer vintage
containing only matured titles produces an explicitly empty active set rather
than falling back to an older cohort. If no valid database vintage exists at or
before the capture cutoff, the baseline is reported as unavailable with metadata
diagnostics and cannot be treated as complete coverage.
