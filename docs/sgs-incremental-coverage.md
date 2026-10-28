# SGS incremental coverage policy (Phase 3B2B)

This is a versioned **engineering policy**, not a BCB publisher guarantee.
`sgs-coverage-policy-v3` plans one `recent` scope per active SGS series in the
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

Frozen v1 and v2 plans retain their exact JSON/hash/bounds and do not gain inferred
availability facts. Their status reports availability as `not_recorded`. New v3
plans freeze the code-owned per-series availability facts and evidence hash with
each unit. A frozen v3 plan is resumed from its snapshot, not rebuilt from current
registry globals. Unknown versions, malformed facts, unsupported registry hashes,
or code/date mismatches degrade plan status rather than assuming a broad default.
Receipt and commit inventories support v1/v2/v3; old proof can attest a v3 unit
only when the current semantic contract, identity, role, and exact requested bounds
match. Policy version alone neither invalidates an exact old capture nor turns it
into a new capture. In particular, legacy SGS 1207 acquisition-contract-v1 proof
does not attest the current annual v2 contract.

## Documented availability boundary (registry v1)

The reviewed registry `bcb-sgs-start-periods-v1` (SHA-256
`b3caac533783a316c7756b1bebd57ea7c701613422a209d03f6b07abec43065e`) contains
all 64 active provider codes. It is based on the BCB SGS public Basic Data page
inspected on 2026-10-28, not a runtime metadata request. The selection URL is
`https://www3.bcb.gov.br/sgspub/consultarmetadados/consultarMetadadosSeries.do?hdOidSerieSelecionada={code}&method=consultarMetadadosSeriesInternet`;
the linked Basic Data frame is
`https://www3.bcb.gov.br/sgspub/JSP/consultarmetadados/cmiDadosBasicos.jsp`.
Dates below reproduce the displayed `DD/MM/YYYY` documented start. The source
page's `dd/MM/yyyy` format was checked against daily values; they are not US-style
month/day dates.

```text
1=28/11/1984  11=04/06/1986  12=06/03/1986  188=01/04/1979
189=01/06/1989  190=01/02/1944  192=01/02/1944  226=01/02/1991
432=05/03/1999  433=01/01/1980  1207=31/12/1962  1635=01/01/1991
1636=01/01/1991  1637=01/01/1991  1638=01/01/1991  1639=01/08/1999
1640=01/08/1999  1641=01/01/1991  1642=01/01/1991  1643=01/08/1999
1788=31/01/1946  1835=31/07/1988  3546=31/12/1970  4447=01/01/1991
4448=01/01/1991  4449=01/01/1991  4466=01/12/1991  4513=01/12/2001
4639=31/01/1991  5727=01/11/2002  5760=01/11/2002  5793=01/11/2002
7478=01/05/2000  10813=28/11/1984  10841=01/01/1991  10842=01/01/1991
10843=01/01/1991  10844=01/01/1991  11428=01/01/1991  13522=01/12/1980
13762=01/12/2006  16122=01/01/1995  20716=01/03/2011  21084=01/03/2011
21619=31/12/1998  22707=01/01/1995  22885=01/01/1995  22905=01/01/1995
23079=01/01/1995  23080=01/01/1995  24363=01/01/2003  24364=01/01/2003
24369=01/03/2012  24382=01/03/2012  25353=01/01/2013  27574=01/01/1998
27788=03/12/2001  27810=31/07/2001  29034=01/03/2005  29037=01/01/2005
29602=01/01/2003  29604=01/01/2003  29606=01/01/2003  29608=01/01/2003
```

For monthly and moving-quarter series, a non-first-day documented date identifies
the first day of that calendar-month reference period (for example 27788's
03/12/2001 becomes 2001-12-01). Daily/market codes and TR 226 retain the exact
documented day. Annual 1207's 1962-12-31 is conservatively normalized to the
1962-01-01 reference year, but its approved group boundary remains 2015; neither
fact authorizes a 1962 request or guarantees an observation in that year. The
availability floor is the later of the frozen approved group start and the
documented first reference period. Thus 1635's required-history boundary is
1991-01-01, 1639 starts with a partial 1999-08 through 1999-12 calendar-year unit,
4466 starts with December 1991, fiscal 13762 is bounded by December 2006, and
credit 25353 by January 2013. Later conflicting CKAN-indexed historical metadata
was not used to truncate backcasts; the earlier current Basic Data period is
conservative. These are scope boundaries, not coverage or publication guarantees.

The v3 public `archive.availability` projection reports the frozen registry
version/hash, source links, per-series raw and normalized dates, approved boundary,
excluded pre-documentation ranges, and conflicts with earlier **receipt-backed**
references. Excluded scopes are not work items, verified archive units, successful
empty responses, fresh observations, or network calls; they do not inflate the
selected archive count (still at most four). A 404 is never evidence to alter the
registry. If a valid committed capture proves an earlier reference than the
documented floor, the projection reports `conflict_requires_review`; planning
does not delete rows, expand its scope, or erase old history. Legacy v1/v2 runs
report `not_recorded`, never a current-registry backfill presented as a fact about
their original plan. GET/status paths do not fetch either metadata URL.

Registry facts do not approve economic names, units, equivalence, licensing, or
source trust. Separate known catalog mismatches remain out of scope for this
availability fix: code 1's USD-sale metadata versus the app's buy label and
10813's USD-buy metadata versus the app's sell label; code 1788's monetary-base
unit in thousands versus the app's M0-in-millions label; code 1835's savings
deposits versus the app's monetary-base label; and codes 27788/27810 units in
thousands versus app labels in millions. Do not relabel, rescale, or change any
indicator formula under this date-boundary change. Candidate-source approvals
and rights remain unchanged and inactive.

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
