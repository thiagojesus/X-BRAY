# Refresh recovery and bounded writes

## Scope and deployment status

This documents the implemented refresh-run recovery, extraction-attempt lifecycle,
and Treasury cohort persistence. The behavior is covered by local unit and
PostgreSQL integration tests; it does **not** establish the historical cause of
any production incident or validate a production database/provider. Migrations
017 and 018 are additive, have been exercised on disposable PostgreSQL 17, and
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

## Current manifest and bounded work

`daily-refresh-v5` is the current manifest. A local date-fixed construction
(2026-10-27) produced 161 work items, including 84 inflation-window items:
12 calendar-year windows for each of seven inflation series. Each such item
invokes its bounded SGS window task with a 20-second request timeout; the
workflow's outer limit is 180 minutes, the client advance request timeout is
300 seconds, and the claim lease is 10 minutes. The 84 count is logical work
items, not a claim that every provider attempt will succeed or that retries
cannot occur. There is no unbounded repeat of the whole manifest inside one
item. Existing aggregate caps and default-off/approval controls are unchanged.

New workflows accept only the current manifest. Explicit resumes accept
supported persisted versions v1-v5 and retain the exact item list saved on the
run; they do not mutate the old run into a v5 manifest. New runs apply the
current source-window policy. Legacy retired-source behavior remains as encoded
for its historical manifest versions; no retroactive history rewrite occurs.

## Evidence and limits

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
