# SGS incremental coverage policy (Phase 3B2B)

This is a versioned **engineering policy**, not a BCB publisher guarantee.
`sgs-coverage-policy-v1` plans one `recent` scope per active SGS series in the
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
annual series. SGS 226 (TR) explicitly uses a 90-day request window while
retaining its full start/end-period identity. Recent starts never extend earlier
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
