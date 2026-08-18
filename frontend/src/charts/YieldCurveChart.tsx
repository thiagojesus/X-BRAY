import { useState, useMemo } from 'react'
import { LineChart, Line, XAxis, YAxis, CartesianGrid, Tooltip, Legend, ResponsiveContainer } from 'recharts'

export interface DiPoint {
  symbol: string
  maturity: string
  rate: number | null
}

export interface CurvasDiData {
  source: string
  days: number
  dates: string[]
  curves: Record<string, DiPoint[]>
}

const PALETTE = ['#ff6b6b', '#ffa502', '#4ecdc4', '#a29bfe', '#ff9ff3', '#54a0ff', '#ffd32a', '#5f27cd', '#01a3a4', '#b33771']

const QUICK_RANGES = [5, 10, 15, 30]

function parsePt(d: string): Date {
  const [dd, mm, yyyy] = d.split('/').map(Number)
  return new Date(yyyy, mm - 1, dd)
}

function formatMaturity(maturity: string): string {
  const [yyyy, mm] = maturity.split('-')
  return `${mm}/${yyyy.slice(2)}`
}

interface YieldCurveChartProps {
  data: CurvasDiData | null
  height?: number
}

export function YieldCurveChart({ data, height = 400 }: YieldCurveChartProps) {
  const [range, setRange] = useState<number>(5)
  const [from, setFrom] = useState('')
  const [to, setTo] = useState('')

  const dates = data?.dates ?? []

  const shownDates = useMemo(() => {
    if (!dates.length) return []
    if (from || to) {
      const fromDate = from ? parsePt(from) : null
      const toDate = to ? parsePt(to) : null
      return dates.filter(d => {
        const dt = parsePt(d)
        if (fromDate && dt < fromDate) return false
        if (toDate && dt > toDate) return false
        return true
      })
    }
    return dates.slice(-Math.min(range, dates.length))
  }, [dates, range, from, to])

  const chartData = useMemo(() => {
    if (!data || !shownDates.length) return []
    const rows: Record<string, any>[] = []
    const maturitySet = new Set<string>()
    for (const d of shownDates) {
      for (const p of data.curves[d] ?? []) {
        maturitySet.add(p.maturity)
      }
    }
    const maturities = Array.from(maturitySet).sort()
    for (const m of maturities) {
      const row: Record<string, any> = { maturity: formatMaturity(m) }
      for (const d of shownDates) {
        const pt = (data.curves[d] ?? []).find(p => p.maturity === m)
        row[d] = pt && pt.rate != null ? pt.rate : undefined
      }
      rows.push(row)
    }
    return rows
  }, [data, shownDates])

  if (!data || !dates.length) {
    return (
      <div className="chart-container">
        <div className="chart-header">
          <h3 className="chart-title">Curvas de Juros Futuros (DI)</h3>
        </div>
        <div className="chart-empty">Sem dados disponíveis</div>
      </div>
    )
  }

  const colorOf = (d: string) => PALETTE[shownDates.indexOf(d) % PALETTE.length]

  const tooltipFormatter = (value: any, name: string) => {
    const num = parseFloat(value)
    if (isNaN(num)) return [value, name]
    return [`${num.toLocaleString('pt-BR', { minimumFractionDigits: 2, maximumFractionDigits: 2 })}% a.a.`, name]
  }

  return (
    <div className="chart-container">
      <div className="chart-header">
        <h3 className="chart-title">Curvas de Juros Futuros (DI)</h3>
      </div>
      <div className="series-controls">
        <div className="series-toggles">
          {QUICK_RANGES.map(n => (
            <button
              key={n}
              className={`tw-btn ${!from && !to && range === n ? 'active' : ''}`}
              onClick={() => {
                setRange(n)
                setFrom('')
                setTo('')
              }}
            >
              {n} dias
            </button>
          ))}
          <input
            type="date"
            className="hist-select"
            aria-label="Data inicial"
            value={from}
            onChange={e => setFrom(e.target.value)}
          />
          <span className="chart-title">a</span>
          <input
            type="date"
            className="hist-select"
            aria-label="Data final"
            value={to}
            onChange={e => setTo(e.target.value)}
          />
          {(from || to) && (
            <button
              className="toggle-action"
              onClick={() => {
                setFrom('')
                setTo('')
                setRange(5)
              }}
            >
              Limpar
            </button>
          )}
        </div>
      </div>
      <ResponsiveContainer width="100%" height={height}>
        <LineChart data={chartData} margin={{ top: 5, right: 30, left: 20, bottom: 5 }}>
          <CartesianGrid strokeDasharray="3 3" stroke="#333" />
          <XAxis dataKey="maturity" tick={{ fontSize: 11, fill: '#999' }} />
          <YAxis
            tick={{ fontSize: 11, fill: '#999' }}
            domain={['dataMin - 0.2', 'dataMax + 0.2']}
            label={{ value: '% a.a.', angle: -90, position: 'insideLeft', fill: '#999' }}
          />
          <Tooltip
            contentStyle={{ background: '#1a1a2e', border: '1px solid #333', borderRadius: 8, color: '#eee' }}
            labelStyle={{ color: '#ccc' }}
            formatter={tooltipFormatter}
          />
          <Legend />
          {shownDates.map(d => (
            <Line
              key={d}
              type="monotone"
              dataKey={d}
              name={d}
              stroke={colorOf(d)}
              strokeWidth={2}
              dot={false}
              connectNulls
              activeDot={{ r: 4 }}
            />
          ))}
        </LineChart>
      </ResponsiveContainer>
    </div>
  )
}
