import { LineChart, Line, XAxis, YAxis, CartesianGrid, Tooltip, Legend, ResponsiveContainer } from 'recharts'

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
}

export function TimeSeriesChart({ data, series, title, yLabel, height = 350 }: TimeSeriesChartProps) {
  if (!data || data.length === 0) {
    return <div className="chart-empty">Sem dados disponíveis</div>
  }

  return (
    <div className="chart-container">
      {title && <h3 className="chart-title">{title}</h3>}
      <ResponsiveContainer width="100%" height={height}>
        <LineChart data={data} margin={{ top: 5, right: 30, left: 20, bottom: 5 }}>
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
  const { BarChart, Bar, XAxis, YAxis, CartesianGrid, Tooltip, Legend, ResponsiveContainer } = require('recharts')

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
