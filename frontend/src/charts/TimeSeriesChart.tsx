import { useState, useMemo } from 'react'
import { LineChart, Line, BarChart, Bar, XAxis, YAxis, CartesianGrid, Tooltip, Legend, ResponsiveContainer } from 'recharts'

interface SeriesConfig {
  key: string
  name: string
  color: string
}

interface TimeSeriesChartProps {
  data: Record<string, any>[]
  series: SeriesConfig[]
  title?: string
  yLabel?: string
  height?: number
  defaultWindow?: string
}

const WINDOWS = [
  { key: '1M', months: 1 },
  { key: '3M', months: 3 },
  { key: '6M', months: 6 },
  { key: '1Y', months: 12 },
  { key: '5Y', months: 60 },
  { key: 'ALL', months: Infinity },
]

function filterByWindow(data: Record<string, any>[], months: number): Record<string, any>[] {
  if (months === Infinity || data.length === 0) return data
  const lastDate = data[data.length - 1]?.date
  if (!lastDate) return data
  const parts = lastDate.split('-')
  const last = new Date(parseInt(parts[0]), parseInt(parts[1]) - 1, parseInt(parts[2]))
  const cutoff = new Date(last)
  cutoff.setMonth(cutoff.getMonth() - months)
  return data.filter(d => {
    const p = d.date.split('-')
    const dt = new Date(parseInt(p[0]), parseInt(p[1]) - 1, parseInt(p[2]))
    return dt >= cutoff
  })
}

export function TimeSeriesChart({ data, series, title, yLabel, height = 350, defaultWindow = 'ALL' }: TimeSeriesChartProps) {
  const [windowKey, setWindowKey] = useState(defaultWindow)
  const months = WINDOWS.find(w => w.key === windowKey)?.months ?? Infinity

  const filtered = useMemo(() => filterByWindow(data, months), [data, months])

  if (!data || data.length === 0) {
    return <div className="chart-empty">Sem dados disponíveis</div>
  }

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
            tickFormatter={(v) => {
              const parts = v.split('-')
              return parts.length === 3 ? `${parts[1]}/${parts[0].slice(2)}` : v
            }}
          />
          <YAxis tick={{ fontSize: 11, fill: '#999' }} label={yLabel ? { value: yLabel, angle: -90, position: 'insideLeft', fill: '#999' } : undefined} />
          <Tooltip
            contentStyle={{ background: '#1a1a2e', border: '1px solid #333', borderRadius: 8, color: '#eee' }}
            labelStyle={{ color: '#ccc' }}
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
              activeDot={{ r: 4 }}
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
