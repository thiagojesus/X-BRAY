# SGS incremental coverage policy (Phase 3B2B)

This is a versioned **engineering policy**, not a BCB publisher guarantee.
`sgs-coverage-policy-v2` plans one `recent` scope per active SGS series in the
approved daily catalog, then at most four global archive units per run. At the
fixed example date 2026-10-27, the pure planner yields 64 recent units plus four
historical maintenance units (68 total). New v6 runs freeze and dispatch those
units in place of the legacy SGS fan-out. The current builder yields 77 items
with both optional phases: 68 SGS units, six unchanged non-SGS source tasks, and
three optional/derived phases. This is a builder result, not a hardcoded count;
the prior legacy-window builder produced 163 with optional phases (161 without).
V6 status reads the frozen plan and a bounded, batched inventory. Frozen group and
freshness metadata are used for resumed plans; the current catalog does not redefine
old run membership. The endpoint performs no provider requests or writes. Legacy
v1-v5 status retains its existing projection.

Engineering lookbacks are 90 calendar days for daily/market data, 24 calendar
months for monthly/moving-quarter/quarterly series, and six calendar years for
annual series. Monthly/moving-quarter starts align to the first of their shifted
month; quarterly starts align to the first of the shifted date's calendar quarter;
annual starts align to January 1 of the shifted year. New windows end at the
actual frozen `as_of`, not a future period boundary. SGS 226 (TR) explicitly
uses a 90-day request window while retaining its full start/end-period identity.
Recent starts never extend earlier
than the existing approved runtime history start: ordinary interest, inflation,
activity, exchange, external, and complementary groups use 2015; fiscal uses
2001; credit uses 2011; approved IPCA catalog groups use their approved history.
Unknown group/frequency policy fails closed. Archive units are no longer than
one calendar year and historical units are selected after all recent units,
with unverified scopes ahead of verified-due scopes and series rotation.

Current recent data availability, validated capture time, and network refresh
count are reported separately. An old cache is not a new capture; missing or
invalid proof remains unverified. Group SLA deadlines follow frozen freshness
metadata rather than assuming monthly data must arrive today. Selected archive
verification is reported independently from the unselected bounded rotation queue;
an actual selected-scope failure or deferral remains visible and can keep the run
partial. Historical annual v1-v5 rows remain diagnostics, not current-plan units.

Only a frozen unit and a matching, recognized-contract successful network
extraction receipt can become verified archive coverage. Cache hits, deferrals,
failures, row min/max dates, row counts, and legacy business rows alone are not
proof. No expected observations are synthesized for weekends, holidays, or
non-release periods. Receipts are bound to operation identity and payload hash;
replaying the same proof is idempotent and a conflicting payload is rejected.
Archive rows are retained and historical values are never rewritten by this
planner. `captured_at` is observation capture time, not publisher publication
time. These mechanics are local policy and test results, not evidence that a
publisher has resolved any production data issue.

Frozen policy-v1 plans and their exact JSON/hash/bounds remain readable and are
not regenerated. Receipt and commit inventories explicitly support v1 and v2;
v1 archive proof is reused only for its exact old identity and bounds. Unknown
versions fail closed. A v2 plan may carry an exact v1 archive intent, but its
recent units must use v2.

The observed primary BCB response for SGS 188 (monthly INPC) requested from
27/10/2024 began with `01/10/2024` / `0.61`, then `01/11/2024` / `0.33`; the
observed final row was `01/09/2026` / `0.82` (24 rows total). This is the sampled
official response, not a complete transcription in this document. Contract
tests use synthetic calendar-boundary rows for quarterly and annual schedules;
they are not additional observations from the provider. The lookback policy is
not a BCB revision or publication guarantee.

For TR, status projects `current_reference_max` from the latest eligible
interval start, never its interval end or the minimum start of a full receipt
window. Eligibility is bounded by the frozen request end (`as_of`) and interval
rows must exist; legacy date-only SGS rows do not attest interval identity. The
optional `series_period_end_max` map reports interval ends separately, keyed by
series code. `series_reference_freshness` compares eligible reference starts
against the frozen series freshness window; capture freshness remains separately
based on the validated capture timestamp. Neither field implies an official
publication date. The projection uses one batched read-only inventory query and
does not rewrite receipts or observations.
