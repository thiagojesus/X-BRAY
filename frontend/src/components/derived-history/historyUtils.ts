import { DERIVED_HISTORY_KEYS, type DerivedHistoryKey, type DerivedHistoryPoint, type DerivedHistoryResponse } from './schema'

export type DerivedHistoryUnit = 'pct_annual' | 'ratio' | 'pp'

export type DerivedHistorySeries = {
  readonly key: DerivedHistoryKey
  readonly label: string
  readonly unit: DerivedHistoryUnit
  readonly color: string
}

export const DERIVED_HISTORY_SERIES = [
  { key: 'juro_real_ex_ante_1y', label: 'Juro real ex-ante de um ano', unit: 'pct_annual', color: '#4ecdc4' },
  { key: 'cobertura_externa', label: 'Cobertura externa', unit: 'ratio', color: '#7b8cff' },
  { key: 'variacao_saldo_primario_12m', label: 'Variação do saldo primário · 12 meses', unit: 'pp', color: '#41d9a0' },
  { key: 'spread_credito_pf_selic', label: 'Spread de crédito PF sobre Selic', unit: 'pp', color: '#ffd166' },
  { key: 'distancia_focus_meta', label: 'Distância Focus à meta', unit: 'pp', color: '#ff6b6b' },
  { key: 'inclinacao_di_5y_1y', label: 'Inclinação DI · 5 anos menos 1 ano', unit: 'pp', color: '#7b8cff' },
] as const satisfies readonly DerivedHistorySeries[]

export const DERIVED_HISTORY_UNITS: readonly { readonly value: DerivedHistoryUnit; readonly label: string }[] = [
  { value: 'pp', label: 'Pontos percentuais (pp)' },
  { value: 'pct_annual', label: 'Taxas anuais (% a.a.)' },
  { value: 'ratio', label: 'Proporção (razão)' },
]

export type DerivedHistoryRow = {
  readonly month: string
  readonly points: Readonly<Partial<Record<DerivedHistoryKey, DerivedHistoryPoint>>>
}

function monthIndex(month: string): number {
  const [year, monthNumber] = month.split('-').map(Number)
  return year * 12 + monthNumber - 1
}

function monthLabel(month: string): string {
  const [year, monthNumber] = month.split('-').map(Number)
  return new Date(Date.UTC(year, monthNumber - 1, 1)).toLocaleDateString('pt-BR', {
    month: 'short',
    year: '2-digit',
    timeZone: 'UTC',
  })
}

export function derivedHistoryMonths(window: DerivedHistoryResponse['window']): readonly string[] {
  const start = monthIndex(window.start_month)
  const end = monthIndex(window.end_month)
  return Array.from({ length: Math.max(0, end - start + 1) }, (_, offset) => {
    const index = start + offset
    const year = Math.floor(index / 12)
    const month = index % 12 + 1
    return `${year}-${String(month).padStart(2, '0')}`
  })
}

export function buildDerivedHistoryRows(history: DerivedHistoryResponse): readonly DerivedHistoryRow[] {
  const byMonth = new Map<string, Partial<Record<DerivedHistoryKey, DerivedHistoryPoint>>>()
  DERIVED_HISTORY_KEYS.forEach(key => {
    history.data[key].forEach(point => {
      const row = byMonth.get(point.month) ?? {}
      row[key] = point
      byMonth.set(point.month, row)
    })
  })

  return derivedHistoryMonths(history.window).map(month => ({ month, points: byMonth.get(month) ?? {} }))
}

export function formatDerivedHistoryMonth(month: string): string {
  return monthLabel(month)
}

export function formatDerivedHistoryValue(value: number | null | undefined, unit: DerivedHistoryUnit): string {
  if (value === null || value === undefined) return 'Sem valor'
  const formatted = value.toLocaleString('pt-BR', { maximumFractionDigits: 3 })
  if (unit === 'pct_annual') return `${formatted}% a.a.`
  if (unit === 'ratio') return formatted
  return `${formatted} pp`
}

export function formatDerivedHistoryCutoff(value: string): string {
  const timestamp = new Date(value).toLocaleString('pt-BR', {
    dateStyle: 'short',
    timeStyle: 'medium',
    timeZone: 'UTC',
  })
  return `${timestamp} UTC (as_of: ${value})`
}

export function derivedHistorySeries(unit: DerivedHistoryUnit): readonly DerivedHistorySeries[] {
  return DERIVED_HISTORY_SERIES.filter(series => series.unit === unit)
}
