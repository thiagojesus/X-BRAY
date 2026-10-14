import { lazy } from 'react'
import {
  type DerivedHistoryRow,
  type DerivedHistorySeries,
  type DerivedHistoryUnit,
  formatDerivedHistoryCutoff,
  formatDerivedHistoryMonth,
  formatDerivedHistoryValue,
} from './historyUtils'
import type { DerivedHistoryKey } from './schema'

type ChartDatum = {
  readonly month: string
  readonly references: Partial<Record<DerivedHistoryKey, string>>
  readonly cutoffs: Partial<Record<DerivedHistoryKey, string>>
} & Partial<Record<DerivedHistoryKey, number | null>>

type DerivedHistoryChartProps = {
  readonly rows: readonly DerivedHistoryRow[]
  readonly series: readonly DerivedHistorySeries[]
  readonly unit: DerivedHistoryUnit
}

function chartData(rows: readonly DerivedHistoryRow[], series: readonly DerivedHistorySeries[]): ChartDatum[] {
  return rows.map(row => {
    const datum: ChartDatum = { month: row.month, references: {}, cutoffs: {} }
    series.forEach(item => {
      const point = row.points[item.key]
      datum[item.key] = point?.snapshot.value ?? null
      if (point !== undefined) {
        datum.references[item.key] = point.snapshot.reference_date
        datum.cutoffs[item.key] = point.snapshot.as_of
      }
    })
    return datum
  })
}

type SnapshotDotProps = {
  readonly cx?: number
  readonly cy?: number
  readonly payload?: ChartDatum
  readonly series: DerivedHistorySeries
}

function SnapshotDot({ cx, cy, payload, series }: SnapshotDotProps) {
  const value = payload?.[series.key]
  const referenceDate = payload?.references[series.key]
  const cutoff = payload?.cutoffs[series.key]
  if (cx === undefined || cy === undefined || payload === undefined || value === null || value === undefined || referenceDate === undefined || cutoff === undefined) {
    return <g />
  }

  return (
    <circle
      className="recharts-dot derived-history-dot"
      cx={cx}
      cy={cy}
      r={3}
      fill={series.color}
      stroke={series.color}
      strokeWidth={1}
      aria-label={`${series.label}: ${formatDerivedHistoryValue(value, series.unit)}. Referência: ${referenceDate}.`}
    >
      <title>{`${formatDerivedHistoryMonth(payload.month)} · ${series.label}: ${formatDerivedHistoryValue(value, series.unit)} · Referência: ${referenceDate} · Corte: ${formatDerivedHistoryCutoff(cutoff)}`}</title>
    </circle>
  )
}

const RechartsHistoryChart = lazy(async () => {
  const { CartesianGrid, Line, LineChart, ResponsiveContainer, XAxis, YAxis } = await import('recharts')
  const Chart = ({ rows, series, unit }: DerivedHistoryChartProps) => {
    const data = chartData(rows, series)
    return (
      <div className="derived-history-chart" data-testid="derived-history-plot" aria-hidden="true">
        <ResponsiveContainer width="100%" height="100%">
          <LineChart data={data} margin={{ top: 8, right: 16, bottom: 4, left: 8 }}>
            <CartesianGrid stroke="var(--border)" strokeDasharray="3 3" />
            <XAxis
              dataKey="month"
              tickFormatter={formatDerivedHistoryMonth}
              minTickGap={24}
              tick={{ fill: 'var(--text-secondary)', fontSize: 11 }}
              axisLine={{ stroke: 'var(--border)' }}
              tickLine={{ stroke: 'var(--border)' }}
            />
            <YAxis
              width={72}
              tickFormatter={value => formatDerivedHistoryValue(Number(value), unit)}
              tick={{ fill: 'var(--text-secondary)', fontSize: 11 }}
              axisLine={{ stroke: 'var(--border)' }}
              tickLine={{ stroke: 'var(--border)' }}
            />
            {series.map(item => (
              <Line
                key={item.key}
                type="monotone"
                dataKey={item.key}
                name={item.label}
                stroke={item.color}
                strokeWidth={2}
                dot={props => <SnapshotDot cx={props.cx} cy={props.cy} payload={props.payload} series={item} />}
                activeDot={{ r: 4 }}
                connectNulls={false}
                isAnimationActive={false}
              />
            ))}
          </LineChart>
        </ResponsiveContainer>
      </div>
    )
  }
  return { default: Chart }
})

export function DerivedHistoryChart(props: DerivedHistoryChartProps) {
  return <RechartsHistoryChart {...props} />
}
