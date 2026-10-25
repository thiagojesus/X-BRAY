# Source ingestion architecture

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
