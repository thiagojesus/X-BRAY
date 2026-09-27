import type {
  ParsedSeriesPoint,
  RawSeriesPoint,
  SeriesMap,
  SeriesRow,
  SeriesTransforms,
  SeriesValue,
  ValidSeriesPoint,
} from '../types/indicators'

type ParsedDate = {
  readonly date: string
  readonly monthKey: string
  readonly sortKey: number
}

type MutableSeriesRow = {
  [key: string]: string | number
  date: string
}

type MutableMonthlySeriesRow = {
  [key: string]: string | number | null
  date: string
}

export type MonthlySeriesRow = Readonly<MutableMonthlySeriesRow>

const DATE_PATTERN = /^(\d{2})\/(\d{2})\/(\d{4})$/
const DECIMAL_PATTERN = /^[+-]?(?:\d+(?:[.,]\d+)?|[.,]\d+)$/

function isRawSeriesPoint(value: unknown): value is RawSeriesPoint {
  if (typeof value !== 'object' || value === null) return false
  if (!('data' in value) || !('valor' in value)) return false
  return typeof value.data === 'string' && (
    value.valor === null ||
    value.valor === undefined ||
    typeof value.valor === 'string' ||
    typeof value.valor === 'number'
  )
}

export function parseSeriesDate(value: string): ParsedDate | null {
  const match = DATE_PATTERN.exec(value)
  if (match === null) return null

  const day = Number(match[1])
  const month = Number(match[2])
  const year = Number(match[3])
  if (year < 1) return null

  const date = new Date(Date.UTC(year, month - 1, day))
  if (
    date.getUTCFullYear() !== year ||
    date.getUTCMonth() !== month - 1 ||
    date.getUTCDate() !== day
  ) {
    return null
  }

  return {
    date: value,
    monthKey: `${match[3]}-${match[2]}`,
    sortKey: year * 10_000 + month * 100 + day,
  }
}

export function parseFiniteSeriesValue(value: SeriesValue): number | null {
  if (value === null || value === undefined) return null
  if (typeof value === 'number') return Number.isFinite(value) ? value : null

  const normalized = value.trim()
  if (!DECIMAL_PATTERN.test(normalized)) return null
  const parsed = Number(normalized.replace(',', '.'))
  return Number.isFinite(parsed) ? parsed : null
}

export function mergeSeriesByDate(
  series: SeriesMap,
  transforms: SeriesTransforms = {},
): SeriesRow[] {
  const rows = new Map<number, MutableSeriesRow>()

  for (const [key, points] of Object.entries(series)) {
    for (const point of points ?? []) {
      if (!isRawSeriesPoint(point)) continue
      const date = parseSeriesDate(point.data)
      const value = parseFiniteSeriesValue(point.valor)
      if (date === null || value === null) continue

      const transformed = transforms[key]?.(value) ?? value
      if (!Number.isFinite(transformed)) continue

      const row = rows.get(date.sortKey) ?? { date: date.date }
      if (row[key] === undefined) row[key] = transformed
      rows.set(date.sortKey, row)
    }
  }

  return [...rows.entries()]
    .sort(([left], [right]) => left - right)
    .map(([_sortKey, row]) => row)
}

export function lastValidPoint(points: readonly RawSeriesPoint[] | undefined): ParsedSeriesPoint | null {
  const latest = lastValidSeriesPoint(points)
  return latest === null ? null : { date: latest.data, value: latest.value }
}

export function lastValidSeriesPoint(
  points: readonly RawSeriesPoint[] | undefined,
): ValidSeriesPoint | null {
  let latest: (ValidSeriesPoint & { readonly sortKey: number }) | null = null

  for (const point of points ?? []) {
    if (!isRawSeriesPoint(point)) continue
    const rawValue = point.valor
    if (rawValue === null || rawValue === undefined) continue
    const date = parseSeriesDate(point.data)
    const value = parseFiniteSeriesValue(rawValue)
    if (date === null || value === null) continue
    if (latest === null || date.sortKey >= latest.sortKey) {
      latest = { data: date.date, valor: rawValue, value, sortKey: date.sortKey }
    }
  }

  return latest === null
    ? null
    : { data: latest.data, valor: latest.valor, value: latest.value }
}

export function groupByMonthLastValue(
  points: readonly RawSeriesPoint[] | undefined,
): readonly ParsedSeriesPoint[] {
  const byMonth = new Map<string, ParsedSeriesPoint & { readonly sortKey: number }>()

  for (const point of points ?? []) {
    if (!isRawSeriesPoint(point)) continue
    const date = parseSeriesDate(point.data)
    const value = parseFiniteSeriesValue(point.valor)
    if (date === null || value === null) continue

    const current = byMonth.get(date.monthKey)
    if (current === undefined || date.sortKey >= current.sortKey) {
      byMonth.set(date.monthKey, { date: date.date, value, sortKey: date.sortKey })
    }
  }

  return [...byMonth.values()]
    .sort((left, right) => left.sortKey - right.sortKey)
    .map(({ date, value }) => ({ date, value }))
}

export const groupMonthlyLast = groupByMonthLastValue

export function mergeSeriesByMonthLastValue(series: SeriesMap): MonthlySeriesRow[] {
  const seriesKeys = Object.keys(series)
  const rows = new Map<string, MutableMonthlySeriesRow>()

  for (const key of seriesKeys) {
    for (const point of groupByMonthLastValue(series[key])) {
      const parsedDate = parseSeriesDate(point.date)
      if (parsedDate === null) continue

      const [year, month] = parsedDate.monthKey.split('-')
      const row = rows.get(parsedDate.monthKey) ?? { date: `01/${month}/${year}` }
      row[key] = point.value
      row[`${key}ReferenceDate`] = point.date
      rows.set(parsedDate.monthKey, row)
    }
  }

  return [...rows.entries()]
    .sort(([left], [right]) => left.localeCompare(right))
    .map(([_month, row]) => {
      for (const key of seriesKeys) {
        row[key] ??= null
        row[`${key}ReferenceDate`] ??= null
      }
      return row
    })
}
