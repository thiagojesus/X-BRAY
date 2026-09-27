export type SeriesFormat = 'pct' | 'brl' | 'usd' | 'idx'

export type TickPeriod = 'year' | 'month' | 'day'

type TooltipEntry = {
  readonly dataKey?: string | number
  readonly payload?: Readonly<Record<string, unknown>>
}

type TooltipValue = string | number | readonly (string | number)[] | null | undefined

type TickOptions = {
  readonly forceYear?: boolean
}

function parseDate(value: string): Date {
  if (value.includes('-')) {
    const parts = value.split('-')
    return new Date(parseInt(parts[0], 10), parseInt(parts[1], 10) - 1, parseInt(parts[2], 10))
  }
  const parts = value.split('/')
  return new Date(parseInt(parts[2], 10), parseInt(parts[1], 10) - 1, parseInt(parts[0], 10))
}

function dateFromRow(row: object): string | undefined {
  if (!('date' in row)) return undefined
  return typeof row.date === 'string' ? row.date : undefined
}

export function formatValue(value: number, format?: SeriesFormat): string {
  if (format === 'pct') return `${value.toFixed(2)}%`
  if (format === 'brl') return `R$ ${value.toLocaleString('pt-BR', { minimumFractionDigits: 2, maximumFractionDigits: 2 })}`
  if (format === 'usd') return `US$ ${value.toLocaleString('en-US', { minimumFractionDigits: 2, maximumFractionDigits: 2 })}`
  return value.toLocaleString('pt-BR', { minimumFractionDigits: 2, maximumFractionDigits: 2 })
}

export function makeTooltipFormatter(
  formatMap: ReadonlyMap<string, SeriesFormat | undefined>,
  showReferenceDates = false,
) {
  return (value: TooltipValue, name: string, entry?: TooltipEntry) => {
    const numberValue = Number.parseFloat(String(value))
    const dataKey = typeof entry?.dataKey === 'string' ? entry.dataKey : ''
    const referenceDate = dataKey ? entry?.payload?.[`${dataKey}ReferenceDate`] : undefined
    const displayName = showReferenceDates && typeof referenceDate === 'string'
      ? `${name} (ref. ${referenceDate})`
      : name
    if (Number.isNaN(numberValue)) return [value, displayName]
    return [formatValue(numberValue, formatMap.get(name)), displayName]
  }
}

export function makeXTickFormatter(xAxisFormat: string) {
  return (value: string) => {
    const date = parseDate(value)
    if (Number.isNaN(date.getTime())) return value
    if (xAxisFormat === 'year') return String(date.getFullYear())
    return `${String(date.getMonth() + 1).padStart(2, '0')}/${String(date.getFullYear()).slice(2)}`
  }
}

export function detectPeriodicity(dates: readonly string[]): TickPeriod {
  const parsed = dates.map(parseDate).filter(date => !Number.isNaN(date.getTime()))
  if (parsed.length < 2) return 'month'
  const gaps: number[] = []
  for (let index = 1; index < parsed.length; index++) {
    gaps.push((parsed[index].getTime() - parsed[index - 1].getTime()) / 86400000)
  }
  gaps.sort((left, right) => left - right)
  const median = gaps[Math.floor(gaps.length / 2)]
  if (median >= 320) return 'year'
  if (median >= 20) return 'month'
  return 'day'
}

function isoWeekKey(date: Date): string {
  const utcDate = new Date(Date.UTC(date.getFullYear(), date.getMonth(), date.getDate()))
  const dayNumber = utcDate.getUTCDay() || 7
  utcDate.setUTCDate(utcDate.getUTCDate() + 4 - dayNumber)
  const yearStart = new Date(Date.UTC(utcDate.getUTCFullYear(), 0, 1))
  const week = Math.ceil((((utcDate.getTime() - yearStart.getTime()) / 86400000) + 1) / 7)
  return `${utcDate.getUTCFullYear()}-${week}`
}

function uniqueByKey(dates: readonly string[], keyForDate: (date: Date) => string): string[] {
  const seen = new Set<string>()
  const uniqueDates: string[] = []
  for (const date of dates) {
    const key = keyForDate(parseDate(date))
    if (!seen.has(key)) {
      seen.add(key)
      uniqueDates.push(date)
    }
  }
  return uniqueDates
}

function sampleTicks(candidates: readonly string[], maximum = 12): string[] {
  const deduplicated = Array.from(new Set(candidates))
  if (deduplicated.length <= maximum) return deduplicated
  const sampled: string[] = []
  for (let index = 0; index < maximum; index++) {
    sampled.push(deduplicated[Math.round(index * (deduplicated.length - 1) / (maximum - 1))])
  }
  return Array.from(new Set(sampled))
}

export function buildXTicks(rows: readonly object[], options?: TickOptions): { ticks: string[]; period: TickPeriod } {
  const valid: { readonly date: string; readonly parsed: Date }[] = []
  for (const row of rows) {
    const date = dateFromRow(row)
    if (date === undefined) continue
    const parsed = parseDate(date)
    if (Number.isNaN(parsed.getTime())) continue
    valid.push({ date, parsed })
  }
  if (valid.length === 0) return { ticks: [], period: 'month' }
  valid.sort((left, right) => left.parsed.getTime() - right.parsed.getTime())

  const dates = valid.map(value => value.date)
  const spanDays = (valid[valid.length - 1].parsed.getTime() - valid[0].parsed.getTime()) / 86400000
  const period = options?.forceYear ? 'year' : detectPeriodicity(dates)

  let candidates: string[]
  let labelPeriod: TickPeriod

  if (period === 'year') {
    candidates = uniqueByKey(dates, date => String(date.getFullYear()))
    labelPeriod = 'year'
  } else if (period === 'month') {
    if (spanDays > 36 * 30) {
      candidates = uniqueByKey(dates, date => String(date.getFullYear()))
      labelPeriod = 'year'
    } else {
      candidates = uniqueByKey(dates, date => `${date.getFullYear()}-${date.getMonth()}`)
      labelPeriod = 'month'
    }
  } else if (spanDays <= 45) {
    candidates = uniqueByKey(dates, date => date.toDateString())
    labelPeriod = 'day'
  } else if (spanDays <= 200) {
    candidates = uniqueByKey(dates, isoWeekKey)
    labelPeriod = 'day'
  } else if (spanDays <= 400) {
    candidates = uniqueByKey(dates, date => `${date.getFullYear()}-${date.getMonth()}`)
    labelPeriod = 'month'
  } else {
    candidates = uniqueByKey(dates, date => String(date.getFullYear()))
    labelPeriod = 'year'
  }

  return { ticks: sampleTicks(candidates), period: labelPeriod }
}

export function makeAdaptiveTickFormatter(period: TickPeriod) {
  return (value: string) => {
    const date = parseDate(value)
    if (Number.isNaN(date.getTime())) return value
    if (period === 'year') return String(date.getFullYear())
    if (period === 'day') return `${String(date.getDate()).padStart(2, '0')}/${String(date.getMonth() + 1).padStart(2, '0')}`
    return `${String(date.getMonth() + 1).padStart(2, '0')}/${String(date.getFullYear()).slice(2)}`
  }
}

export function filterByWindow<Row extends object>(data: Row[], months: number): Row[] {
  if (data.length === 0 || months === Infinity) return data
  const lastDate = dateFromRow(data[data.length - 1])
  if (!lastDate) return data
  const last = parseDate(lastDate)

  if (months === -1) {
    const yearStart = new Date(last.getFullYear(), 0, 1)
    return data.filter(row => {
      const date = dateFromRow(row)
      return date !== undefined && parseDate(date) >= yearStart
    })
  }

  const cutoff = new Date(last)
  cutoff.setMonth(cutoff.getMonth() - months)
  return data.filter(row => {
    const date = dateFromRow(row)
    return date !== undefined && parseDate(date) >= cutoff
  })
}
