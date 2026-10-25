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

The ledger records attempt identity and safe error class, not raw payloads,
provider URLs, query strings, credentials, or licensed source files. Unknown
insert/update counts remain null. A source failure is not represented as a
successful extraction merely because older observations remain available to
cache-only readers.
