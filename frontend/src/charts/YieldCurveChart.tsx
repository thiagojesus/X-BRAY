import { useId, useMemo, useState } from 'react'
import { CartesianGrid, Legend, Line, LineChart, ResponsiveContainer, Tooltip, XAxis, YAxis } from 'recharts'

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
const EMPTY_DATES: readonly string[] = []

function parsePt(value: string): Date | null {
  if (!/^\d{2}\/\d{2}\/\d{4}$/.test(value)) return null
  const day = Number(value.slice(0, 2))
  const month = Number(value.slice(3, 5))
  const year = Number(value.slice(6))
  const date = new Date(0)
  date.setUTCFullYear(year, month - 1, day)
  return year > 0 && date.getUTCFullYear() === year
    && date.getUTCMonth() === month - 1 && date.getUTCDate() === day ? date : null
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
  const dateAlertId = useId()
  const fromDate = useMemo(() => parsePt(from), [from])
  const toDate = useMemo(() => parsePt(to), [to])
  const fromValid = from === '' || fromDate !== null
  const toValid = to === '' || toDate !== null
  const datesValid = fromValid && toValid

  const dates = data?.dates ?? EMPTY_DATES

  const shownDates = useMemo(() => {
    if (!dates.length || !fromValid || !toValid) return []
    if (from || to) {
      return dates.filter(d => {
        const dt = parsePt(d)
        if (!dt) return false
        if (fromDate && dt < fromDate) return false
        if (toDate && dt > toDate) return false
        return true
      })
    }
    return dates.slice(-Math.min(range, dates.length))
  }, [dates, range, from, to, fromDate, toDate, fromValid, toValid])

  const chartData = useMemo(() => {
    if (!data || !shownDates.length) return []
    const rows: Record<string, string | number | undefined>[] = []
    const maturitySet = new Set<string>()
    for (const d of shownDates) {
      for (const p of data.curves[d] ?? []) {
        maturitySet.add(p.maturity)
      }
    }
    const maturities = Array.from(maturitySet).sort()
    for (const m of maturities) {
      const row: Record<string, string | number | undefined> = { maturity: formatMaturity(m) }
      for (const d of shownDates) {
        const pt = (data.curves[d] ?? []).find(p => p.maturity === m)
        row[d] = pt?.rate != null && Number.isFinite(pt.rate) ? pt.rate : undefined
      }
      rows.push(row)
    }
    return rows
  }, [data, shownDates])

  const axis = useMemo(() => {
    const rates = chartData.flatMap(row => Object.values(row))
      .filter((value): value is number => typeof value === 'number' && Number.isFinite(value))
    if (rates.length === 0) return { domain: ['auto', 'auto'], ticks: [] }
    const minimum = Math.min(...rates)
    const maximum = Math.max(...rates)
    const span = maximum - minimum
    const padding = span === 0 ? Math.max(Math.abs(minimum) * 0.01, 0.1) : span * 0.1
    const roughStep = (span + padding * 2) / 5
    const magnitude = 10 ** Math.floor(Math.log10(roughStep))
    const normalizedStep = roughStep / magnitude
    const step = (normalizedStep <= 1 ? 1 : normalizedStep <= 2 ? 2 : normalizedStep <= 5 ? 5 : 10) * magnitude
    const precision = Math.max(0, -Math.floor(Math.log10(step)))
    const firstTick = Math.floor((minimum - padding) / step)
    const lastTick = Math.ceil((maximum + padding) / step)
    const lower = Number((firstTick * step).toFixed(precision))
    const upper = Number((lastTick * step).toFixed(precision))
    const ticks = Array.from({ length: lastTick - firstTick + 1 }, (_, index) => Number(((firstTick + index) * step).toFixed(precision)))
    const formatter = new Intl.NumberFormat('pt-BR', { minimumFractionDigits: precision, maximumFractionDigits: precision })
    return { domain: [lower, upper], ticks, tickFormatter: formatter.format }
  }, [chartData])

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

  const tooltipFormatter = (value: number | string | (number | string)[], name: string) => {
    const num = Number.parseFloat(String(value))
    if (Number.isNaN(num)) return [value, name]
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
              type="button"
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
            type="text"
            className="hist-select"
            aria-label="Data inicial"
            aria-invalid={!fromValid}
            aria-describedby={datesValid ? undefined : dateAlertId}
            placeholder="DD/MM/AAAA"
            title="DD/MM/AAAA"
            size={12}
            maxLength={10}
            value={from}
            onChange={e => setFrom(e.target.value)}
          />
          <span className="chart-title">a</span>
          <input
            type="text"
            className="hist-select"
            aria-label="Data final"
            aria-invalid={!toValid}
            aria-describedby={datesValid ? undefined : dateAlertId}
            placeholder="DD/MM/AAAA"
            title="DD/MM/AAAA"
            size={12}
            maxLength={10}
            value={to}
            onChange={e => setTo(e.target.value)}
          />
          {(from || to) && (
            <button
              type="button"
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
        {!datesValid && <p id={dateAlertId} className="chart-series-unavailable" role="alert">Informe datas válidas no formato DD/MM/AAAA.</p>}
      </div>
      <ResponsiveContainer width="100%" height={height}>
        <LineChart data={chartData} margin={{ top: 5, right: 30, left: 20, bottom: 5 }}>
          <CartesianGrid strokeDasharray="3 3" stroke="#333" />
          <XAxis dataKey="maturity" tick={{ fontSize: 11, fill: '#999' }} />
          <YAxis
            tick={{ fontSize: 11, fill: '#999' }}
            domain={axis.domain}
            ticks={axis.ticks}
            tickFormatter={axis.tickFormatter}
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
