# Refresh recovery and bounded writes

## Scope and deployment status

This documents the implemented refresh-run recovery, extraction-attempt lifecycle,
and Treasury cohort persistence. The behavior is covered by local unit and
PostgreSQL integration tests; it does **not** establish the historical cause of
any production incident or validate a production database/provider. Migrations
017 through 021 are additive, have been exercised on disposable PostgreSQL 17, and
have **not** been applied to production. Apply them through the normal reviewed
deployment process before enabling production code that depends on them.

## Migrations and durable outcomes

Migration `017_treasury_cohort_receipts` adds durable cohort receipts. Treasury
input is parsed and validated as a complete cohort, staged in bounded chunks,
then replacement, receipt, and freshness metadata are committed atomically. A
matching receipt makes replay idempotent; a conflicting operation identity is
rejected. Failure before commit leaves the prior cohort and freshness intact.

Migration `018_refresh_attempt_lifecycle` adds refresh-attempt and milestone
records with nullable historical fields. New work records claim generation,
attempt identity, and milestones. An expired attempt is reconciled as unknown
unless a durable commit receipt proves success. Read-only GET/status paths do
not reconcile or write. A missing durable outcome remains unknown; it is not
reported as a fabricated zero-count success.

Migration `019_refresh_deferred_dispatch` adds monotonic claim generation,
logical acquisition count, scheduler eligibility, deferral reason, and the last
actual provider error code/category. Outbound-start is recorded once per fenced claim
before dispatch; repeated markers are idempotent and stale claim tokens cannot
defer or overwrite a replacement claim.

Migration `020_sgs_incremental_coverage` stores the immutable SGS plan snapshot,
per-run plan units, archive fairness cursor, and receipt-backed archive coverage.
The current v6 manifest freezes and dispatches the bounded plan. Recent coverage,
selected archive verification, and remaining archive rotation are distinct status
facts; only a recognized successful network receipt verifies archive coverage.

Migration `021_treasury_intent_trace` adds nullable, non-backfilled Treasury
receipt bindings (run, task, contract version, official dataset UUID, cohort-scope
digest, and canonical active-row digest) and a bounded safe per-dispatch trace on
source extraction outcomes. A v6 replay requires an exact stable intent match and
the still-active stored cohort to match the durable receipt; claim generation is
not part of that stable intent. Incomplete legacy receipts are not promoted into
proof. HTTP redirects count against the same two-dispatch Treasury logical budget;
429 defers without retrying. Trace rows contain route/host IDs, hop/attempt kind,
timing, status and known byte count, not raw bodies, headers, credentials, or URLs.

Migration 021 also adds `source_access_state.admission_generation`, initialized
to zero for existing control rows. Admission atomically advances and returns this
internal generation; a validated success acknowledgement compares it before
changing breaker state. A later admission or failure fences an older worker,
while repeated acknowledgements from the same admitted logical budget remain
idempotent. Provider capture time supplies the success event time and
`updated_at` is monotonic. The technical default is not historical capture proof
and does not backfill source receipts.

## Current manifest and bounded work

`daily-refresh-v6` is the current manifest. A local date-fixed construction
(2026-10-27) produced 77 work items with the normal optional phases: 68 frozen
SGS units, six unchanged non-SGS source tasks, and three optional/derived phases.
The prior legacy-window builder produced 163 with optional phases (161 without),
including 84 inflation-window items. Each such legacy item
invokes its bounded SGS window task with a 20-second request timeout; the
workflow's outer limit is 180 minutes, the client advance request timeout is
300 seconds, and the claim lease is 10 minutes. The 84 count is logical work
items, not a claim that every provider attempt will succeed or that retries
cannot occur. There is no unbounded repeat of the whole manifest inside one
item. Existing aggregate caps and default-off/approval controls are unchanged.

New workflows accept only the current manifest. Explicit resumes accept
supported persisted versions v1-v5 and retain the exact item list saved on the
run; they do not mutate the old run into a v6 manifest. New runs apply the
current source-window policy. Legacy retired-source behavior remains as encoded
for its historical manifest versions; no retroactive history rewrite occurs.

Provider cooldown and HTTP 429 advice are durable scheduler eligibility, not an
in-call sleep/retry. Eligible work from another publisher can proceed. A refresh
client waits at most 300 seconds total for due deferred work; if the next due
time is outside the remaining run or wait budget, the run finalizes `partial`,
returns the deferred task and retry metadata, and releases its lease. The
terminal run is immutable; a new run uses canonical current work items and
inherits provider eligibility from durable admission state. Public snapshots
add optional `next_due_at` and `due_work_item_key`; deferred final tasks retain
legacy `partial` task status and add `acquisition_status`, `retry_not_before`,
`deferral_reason`, and `error_category`. Read-only status requests do not write.

For v6 completed failures, the same persisted typed category controls client
selection, direct server claims, due-work projection, and finalization. Only
`network_timeout`, `network_reset`, and `temporary_server_error` may use the
existing second logical acquisition. `invalid_schema`, `semantic_conflict`,
`incomplete_coverage`, `envelope_error`, `authentication_error`,
`configuration_error`, and `forbidden_host` resolve the failed item within that
run. Missing or unknown categories are conservative terminal outcomes; they are
not re-downloaded to resolve uncertain persistence/logging stages. Durable
receipt reconciliation remains ahead of new acquisition. V1-v5 dispatch keeps
its prior retry contract. Semantic validation failures do not open publisher
circuits; HTTP 429 and admission cooldown remain durable deferrals. The rule uses
existing nullable category columns and requires no migration or historical row
rewrite.

## Evidence and limits

Engineering lookbacks are policy windows, not publisher revision guarantees:
daily/market recent windows use 90 calendar days (including TR's full period
identity), monthly/quarterly windows use 24 calendar months, annual windows use
six calendar years, and archive maintenance selects at most four units per run.
Recent availability does not verify archive coverage. A validated provider
receipt is separate from a database min/max range, capture timestamp, or
publisher publication time. The selected four archive units are independent of
the recent pass and of global provider quotas; no calendar interpolation or
undocumented holiday/release rule is inferred.

Refresh-status projection only treats a later SGS window as recovery evidence
when its new extraction ledger row explicitly carries the canonical
`acquisition-contract-v1` version, `point` or `full_period` identity kind, and
catalog unit, plus the requested series and bounds, successful network/count
outcome, and a completed work item. These fields are written by the audited SGS
window producer from `source_contracts`; status does not infer missing metadata
for historical rows or mutate legacy task/run outcomes. A historical error stays
visible until a new matching network extraction proves complete persisted
coverage. TR code 226 uses its full period-start/period-end identity and
`pct_period` unit. No provider request is made by the status GET.

Local PostgreSQL 17.11 (`xbray_test`) tests exercise migrations, claim fencing,
receipt-backed replay/rollback, Treasury bulk staging, and unknown outcomes.
The synthetic 180,000-row Treasury test is a local implementation benchmark,
not evidence of a past production timeout or production throughput. Existing
production-source outages and historical failure causes remain unverified.
Production validation requires reviewed deployment of both migrations followed
by ordinary, authorized source health observation; this implementation session
does not perform either action.
