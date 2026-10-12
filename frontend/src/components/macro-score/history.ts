import type { HistorySeries, MacroScorePayload } from './schema'

const SAO_PAULO_MONTH_FORMATTER = new Intl.DateTimeFormat('en-CA', {
  year: 'numeric',
  month: '2-digit',
  timeZone: 'America/Sao_Paulo',
})

export function currentScoreMonth(now: Date): string {
  const parts = SAO_PAULO_MONTH_FORMATTER.formatToParts(now)
  return `${parts.find(part => part.type === 'year')?.value}-${parts.find(part => part.type === 'month')?.value}`
}

export function historyMonths(end: string): readonly string[] {
  const anchor = new Date(`${end}-01T00:00:00Z`)
  return Array.from({ length: 120 }, (_, index) => new Date(Date.UTC(anchor.getUTCFullYear(), anchor.getUTCMonth() - 119 + index, 1)).toISOString().slice(0, 7))
}

export type HistoryPoint = {
  readonly month: string
  readonly value: number | null
  readonly snapshot: MacroScorePayload | null
}

export function historyPoints(months: readonly string[], snapshots: readonly MacroScorePayload[], series: HistorySeries): readonly HistoryPoint[] {
  const byMonth = new Map(snapshots.map(snapshot => [snapshot.score_month, snapshot]))
  return months.map(month => {
    const snapshot = byMonth.get(month) ?? null
    return { month, snapshot, value: snapshot === null ? null : series === 'overall' ? snapshot.score : snapshot.dimensions[series].score }
  })
}
