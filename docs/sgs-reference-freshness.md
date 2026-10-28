# SGS reference freshness: interpretation and cadence audit

## What the status means

`/api/refresh-status` reports two different facts. Capture freshness describes
the age of a verified acquisition; reference freshness describes the age of the
observed period interpreted using the cadence frozen in that run's plan. A
period-end basis is a calendar calculation, not evidence of when BCB published
the value. `current_reference_max` and each series' observed reference remain
the original source labels (for example, `2026-08-01`), not the calculated
period end. The additional `series_reference_freshness_details` diagnostic
reports that basis, deadline, age, UTC as-of date, and evaluation policy.
Deadlines are inclusive: a series is stale only after its deadline. A cached
capture can be fresh while its reference is stale, and vice versa.

This reporting correction does not alter frozen plans, capture receipts,
observations, source-published timestamps, business/score snapshots, or the
historical result of any refresh run. A status GET recalculates the current
assessment; it does not acquire data. An unknown cadence or missing proof must
not be treated as healthy.

## Five credit/debt warnings audited

As of the assessment date **2026-10-28**, the following rows remain stale under
the currently frozen engineering freshness budget. The official material
located establishes that some statistics in the product have different
reference months, but does not prove a per-series release time or that each
series' latest available reference has been captured. Thus `publisher cadence`
is deliberately marked **unknown**, not as an outage or discontinuation.

| SGS code / series | Observed reference | Capture evidence | Frozen budget and calculated basis | Evidence and uncertainty | Appropriate follow-up |
|---|---|---|---|---|---|
| 20716 `taxa_media_credito_pf` | 2026-08-01 | Verified primary capture from run `40613868745`, 2026-10-28 01:28–01:34 UTC window | 45 days; monthly basis 2026-08-31; deadline 2026-10-15; stale | BCB's 29 Sep monetary/credit note says average interest rate “in August”, but the located quote is aggregate, not verified as this exact PF series/value. Exact code mapping and October release time unknown. | After the next official release is established, compare this series' latest source reference and perform a targeted capture only if behind. |
| 21084 `inadimplencia_pf` | 2026-08-01 | Same verified primary-capture window above | 45 days; monthly basis 2026-08-31; deadline 2026-10-15; stale | BCB note says household 90-days-past-due delinquency “in August”; exact code/value equivalence and later release timing not established. | Check the official note/calendar after release; verify this code's latest reference before deciding on a targeted capture. |
| 25353 `icc_pf` | 2026-08-01 | Same verified primary-capture window above | 45 days; monthly basis 2026-08-31; deadline 2026-10-15; stale | Exact series mapping was not verified. The code label may represent a stock/credit-cost measure, not new-loan interest; the generic interest-rate quote is not evidence for this code. | Verify the exact SGS definition and latest official reference; do not infer cadence from the aggregate interest-rate statement. |
| 29034 `comprometimento_renda` | 2026-07-01 | Same verified primary-capture window above | 75 days; monthly basis 2026-07-31; deadline 2026-10-14; stale | BCB note reports household debt service in July, illustrating within-product reference-month differences; exact code mapping and publication timestamp remain unverified. | Check the official release calendar and this code's latest reference after publication; target a capture only if needed. |
| 29037 `endividamento_familias` | 2026-07-01 | Same verified primary-capture window above | 75 days; monthly basis 2026-07-31; deadline 2026-10-14; stale | BCB note says household debt was “in July”; no per-series publication-time or latest-capture proof was established. | Check official release material and this code's latest reference after publication; target a capture only if behind. |

The audit sources are the BCB [29 September 2026 monetary and credit
statistics note](https://www.bcb.gov.br/detalhenoticia/21275/nota), its
[English statistical text](https://www.bcb.gov.br/content/statistics/monetarycreditstatistics_prev/202609_Monetary_and_credit_statistics_text.pdf),
the [monetary and credit statistics calendar](https://www.bcb.gov.br/acessoinformacao/calendariobc?agenda=Estat%C3%ADsticas%20monet%C3%A1rias%20e%20de%20cr%C3%A9dito&tituloAgenda=Calend%C3%A1rio%20das%20Estat%C3%ADsticas%20monet%C3%A1rias%20e%20de%20cr%C3%A9dito%E2%80%8B),
and the September 2026 [Statistics Revision Policy](https://www.bcb.gov.br/content/estatisticas/Documents/notas_metodologicas/Politica_de_Revisoes_de_Estatisticas.pdf).
The policy's monthly revision window of up to one month for broad credit
statistics does not establish an exact release time or these five series'
individual publication cadence. The located 29 September note is not claimed
to be the latest release as of 28 October, and no October release day or time
was verified. Accordingly these five current-policy warnings are retained;
no thresholds, score/business rules, source values, or refresh behavior are
changed by this audit.
