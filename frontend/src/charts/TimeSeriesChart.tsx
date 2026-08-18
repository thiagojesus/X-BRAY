import { useState, useMemo } from 'react'
import { LineChart, Line, BarChart, Bar, XAxis, YAxis, CartesianGrid, Tooltip, Legend, ResponsiveContainer } from 'recharts'

export type SeriesFormat = 'pct' | 'brl' | 'usd' | 'idx'

interface SeriesConfig {
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
    if (xAxisFormat === 'year') {
      const parts = v.split('/')
      return parts.length === 3 ? parts[2] : v
    }
    const parts = v.split('/')
    return parts.length === 3 ? `${parts[1]}/${parts[0].slice(2)}` : v
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

  const xTickFormatter = makeXTickFormatter(xAxisFormat)

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
          <XAxis dataKey="date" tick={{ fontSize: 11, fill: '#999' }} />
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
