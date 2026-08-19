import { useState, useMemo } from 'react'
import { LineChart, Line, BarChart, Bar, XAxis, YAxis, CartesianGrid, Tooltip, Legend, ResponsiveContainer } from 'recharts'

export type SeriesFormat = 'pct' | 'brl' | 'usd' | 'idx'

export interface SeriesConfig {
  key: string
  name: string
  color: string
  yAxisId?: string
  format?: SeriesFormat
}

interface TimeSeriesChartProps {
  data: Record<string, any>[]
  series: SeriesConfig[]
  title?: string
  yLabel?: string
  height?: number
  defaultWindow?: string
  xAxisFormat?: 'date' | 'year'
  yTickFormatter?: (value: number) => string
}

export function formatValue(val: number, format?: SeriesFormat): string {
  if (format === 'pct') return `${val.toFixed(2)}%`
  if (format === 'brl') return `R$ ${val.toLocaleString('pt-BR', { minimumFractionDigits: 2, maximumFractionDigits: 2 })}`
  if (format === 'usd') return `US$ ${val.toLocaleString('en-US', { minimumFractionDigits: 2, maximumFractionDigits: 2 })}`
  return val.toLocaleString('pt-BR', { minimumFractionDigits: 2, maximumFractionDigits: 2 })
}

const WINDOWS = [
  { key: '1M', months: 1 },
  { key: '3M', months: 3 },
  { key: '6M', months: 6 },
  { key: 'YTD', months: -1 },
  { key: '1Y', months: 12 },
  { key: '5Y', months: 60 },
  { key: 'ALL', months: Infinity },
]

function parseDate(d: string): Date {
  if (d.includes('-')) {
    const parts = d.split('-')
    return new Date(parseInt(parts[0]), parseInt(parts[1]) - 1, parseInt(parts[2]))
  }
  const parts = d.split('/')
  return new Date(parseInt(parts[2]), parseInt(parts[1]) - 1, parseInt(parts[0]))
}

export function makeTooltipFormatter(formatMap: Map<string, SeriesFormat | undefined>) {
  return (value: any, name: string) => {
    const num = parseFloat(value)
    if (isNaN(num)) return [value, name]
    return [formatValue(num, formatMap.get(name)), name]
  }
}

export function makeXTickFormatter(xAxisFormat: string) {
  return (v: string) => {
    const d = parseDate(v)
    if (isNaN(d.getTime())) return v
    if (xAxisFormat === 'year') return String(d.getFullYear())
    return `${String(d.getMonth() + 1).padStart(2, '0')}/${String(d.getFullYear()).slice(2)}`
  }
}

export type TickPeriod = 'year' | 'month' | 'day'

/** Detecta a periodicidade dos dados pela mediana dos gaps entre datas consecutivas. */
export function detectPeriodicity(dates: string[]): TickPeriod {
  const parsed = dates.map(parseDate).filter(d => !isNaN(d.getTime()))
  if (parsed.length < 2) return 'month'
  const gaps: number[] = []
  for (let i = 1; i < parsed.length; i++) {
    gaps.push((parsed[i].getTime() - parsed[i - 1].getTime()) / 86400000)
  }
  gaps.sort((a, b) => a - b)
  const median = gaps[Math.floor(gaps.length / 2)]
  if (median >= 320) return 'year'
  if (median >= 20) return 'month'
  return 'day'
}

function isoWeekKey(d: Date): string {
  const date = new Date(Date.UTC(d.getFullYear(), d.getMonth(), d.getDate()))
  const dayNum = date.getUTCDay() || 7
  date.setUTCDate(date.getUTCDate() + 4 - dayNum)
  const yearStart = new Date(Date.UTC(date.getUTCFullYear(), 0, 1))
  const week = Math.ceil((((date.getTime() - yearStart.getTime()) / 86400000) + 1) / 7)
  return `${date.getUTCFullYear()}-${week}`
}

function uniqueByKey(dates: string[], keyFn: (d: Date) => string): string[] {
  const seen = new Set<string>()
  const out: string[] = []
  for (const d of dates) {
    const key = keyFn(parseDate(d))
    if (!seen.has(key)) {
      seen.add(key)
      out.push(d)
    }
  }
  return out
}

function sampleTicks(candidates: string[], max = 12): string[] {
  const dedup = Array.from(new Set(candidates))
  if (dedup.length <= max) return dedup
  const out: string[] = []
  for (let i = 0; i < max; i++) {
    out.push(dedup[Math.round(i * (dedup.length - 1) / (max - 1))])
  }
  return Array.from(new Set(out))
}

/**
 * Gera ticks para o eixo X respeitando a periodicidade dos dados:
 * anual → 1 tick por ano; mensal → 1 por mês (ou por ano se span longo);
 * diário → semanal/DD-MM para spans curtos, mensal para médios, anual para longos.
 */
export function buildXTicks(rows: Record<string, any>[], opts?: { forceYear?: boolean }): { ticks: string[]; period: TickPeriod } {
  const valid: { date: string; d: Date }[] = []
  for (const r of rows) {
    if (typeof r?.date !== 'string' || !r.date) continue
    const d = parseDate(r.date)
    if (isNaN(d.getTime())) continue
    valid.push({ date: r.date, d })
  }
  if (valid.length === 0) return { ticks: [], period: 'month' }
  valid.sort((a, b) => a.d.getTime() - b.d.getTime())

  const dates = valid.map(v => v.date)
  const spanDays = (valid[valid.length - 1].d.getTime() - valid[0].d.getTime()) / 86400000
  const period = opts?.forceYear ? 'year' : detectPeriodicity(dates)

  let candidates: string[]
  let labelPeriod: TickPeriod

  if (period === 'year') {
    candidates = uniqueByKey(dates, d => String(d.getFullYear()))
    labelPeriod = 'year'
  } else if (period === 'month') {
    if (spanDays > 36 * 30) {
      candidates = uniqueByKey(dates, d => String(d.getFullYear()))
      labelPeriod = 'year'
    } else {
      candidates = uniqueByKey(dates, d => `${d.getFullYear()}-${d.getMonth()}`)
      labelPeriod = 'month'
    }
  } else {
    if (spanDays <= 45) {
      candidates = uniqueByKey(dates, d => d.toDateString())
      labelPeriod = 'day'
    } else if (spanDays <= 200) {
      candidates = uniqueByKey(dates, isoWeekKey)
      labelPeriod = 'day'
    } else if (spanDays <= 400) {
      candidates = uniqueByKey(dates, d => `${d.getFullYear()}-${d.getMonth()}`)
      labelPeriod = 'month'
    } else {
      candidates = uniqueByKey(dates, d => String(d.getFullYear()))
      labelPeriod = 'year'
    }
  }

  return { ticks: sampleTicks(candidates, 12), period: labelPeriod }
}

export function makeAdaptiveTickFormatter(period: TickPeriod) {
  return (v: string) => {
    const d = parseDate(v)
    if (isNaN(d.getTime())) return v
    if (period === 'year') return String(d.getFullYear())
    if (period === 'day') return `${String(d.getDate()).padStart(2, '0')}/${String(d.getMonth() + 1).padStart(2, '0')}`
    return `${String(d.getMonth() + 1).padStart(2, '0')}/${String(d.getFullYear()).slice(2)}`
  }
}

export function filterByWindow(data: Record<string, any>[], months: number): Record<string, any>[] {
  if (data.length === 0) return data
  if (months === Infinity) return data
  const lastDate = data[data.length - 1]?.date
  if (!lastDate) return data
  const last = parseDate(lastDate)

  if (months === -1) {
    const yearStart = new Date(last.getFullYear(), 0, 1)
    return data.filter(d => parseDate(d.date) >= yearStart)
  }

  const cutoff = new Date(last)
  cutoff.setMonth(cutoff.getMonth() - months)
  return data.filter(d => parseDate(d.date) >= cutoff)
}

export function TimeSeriesChart({ data, series, title, yLabel, height = 350, defaultWindow = 'YTD', xAxisFormat = 'date', yTickFormatter }: TimeSeriesChartProps) {
  const [windowKey, setWindowKey] = useState(defaultWindow)
  const months = WINDOWS.find(w => w.key === windowKey)?.months ?? Infinity

  const filtered = useMemo(() => filterByWindow(data, months), [data, months])

  const { ticks, period } = useMemo(
    () => buildXTicks(filtered, { forceYear: xAxisFormat === 'year' }),
    [filtered, xAxisFormat]
  )

  if (!data || data.length === 0) {
    return <div className="chart-empty">Sem dados disponíveis</div>
  }

  const hasRightAxis = series.some(s => s.yAxisId === 'right')
  const formatMap = new Map(series.map(s => [s.name, s.format]))

  const tooltipFormatter = (value: any, name: string) => {
    const num = parseFloat(value)
    if (isNaN(num)) return [value, name]
    return [formatValue(num, formatMap.get(name)), name]
  }

  const tooltipLabelFormatter = (label: string) => label

  const xTickFormatter = makeAdaptiveTickFormatter(period)

  return (
    <div className="chart-container">
      <div className="chart-header">
        {title && <h3 className="chart-title">{title}</h3>}
        <div className="time-window">
          {WINDOWS.map(w => (
            <button
              key={w.key}
              className={`tw-btn ${windowKey === w.key ? 'active' : ''}`}
              onClick={() => setWindowKey(w.key)}
            >
              {w.key}
            </button>
          ))}
        </div>
      </div>
      <ResponsiveContainer width="100%" height={height}>
        <LineChart data={filtered} margin={{ top: 5, right: 30, left: 20, bottom: 5 }}>
          <CartesianGrid strokeDasharray="3 3" stroke="#333" />
          <XAxis
            dataKey="date"
            tick={{ fontSize: 11, fill: '#999' }}
            ticks={ticks}
            tickFormatter={xTickFormatter}
          />
          <YAxis yAxisId="left" tick={{ fontSize: 11, fill: '#999' }} tickFormatter={yTickFormatter} label={yLabel ? { value: yLabel, angle: -90, position: 'insideLeft', fill: '#999' } : undefined} />
          {hasRightAxis && (
            <YAxis yAxisId="right" orientation="right" tick={{ fontSize: 11, fill: '#999' }} />
          )}
          <Tooltip
            contentStyle={{ background: '#1a1a2e', border: '1px solid #333', borderRadius: 8, color: '#eee' }}
            labelStyle={{ color: '#ccc' }}
            formatter={tooltipFormatter}
            labelFormatter={tooltipLabelFormatter}
          />
          <Legend />
          {series.map(s => (
            <Line
              key={s.key}
              type="monotone"
              dataKey={s.key}
              name={s.name}
              stroke={s.color}
              strokeWidth={2}
              dot={false}
              connectNulls
              activeDot={{ r: 4 }}
              yAxisId={s.yAxisId || 'left'}
            />
          ))}
        </LineChart>
      </ResponsiveContainer>
    </div>
  )
}

export function BarChartComponent({ data, series, title, height = 350 }: TimeSeriesChartProps) {
  if (!data || data.length === 0) {
    return <div className="chart-empty">Sem dados disponíveis</div>
  }

  const { ticks, period } = useMemo(() => buildXTicks(data), [data])
  const formatMap = new Map(series.map(s => [s.name, s.format]))

  const tooltipFormatter = (value: any, name: string) => {
    const num = parseFloat(value)
    if (isNaN(num)) return [value, name]
    return [formatValue(num, formatMap.get(name)), name]
  }

  return (
    <div className="chart-container">
      {title && <h3 className="chart-title">{title}</h3>}
      <ResponsiveContainer width="100%" height={height}>
        <BarChart data={data} margin={{ top: 5, right: 30, left: 20, bottom: 5 }}>
          <CartesianGrid strokeDasharray="3 3" stroke="#333" />
          <XAxis dataKey="date" tick={{ fontSize: 11, fill: '#999' }} ticks={ticks} tickFormatter={makeAdaptiveTickFormatter(period)} />
          <YAxis tick={{ fontSize: 11, fill: '#999' }} />
          <Tooltip
            contentStyle={{ background: '#1a1a2e', border: '1px solid #333', borderRadius: 8, color: '#eee' }}
            formatter={tooltipFormatter}
          />
          <Legend />
          {series.map((s: SeriesConfig) => (
            <Bar key={s.key} dataKey={s.key} name={s.name} fill={s.color} />
          ))}
        </BarChart>
      </ResponsiveContainer>
    </div>
  )
}
