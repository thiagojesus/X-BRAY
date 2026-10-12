import { lazy } from 'react'
import type { HistoryPoint } from './history'

type ScoreChartProps = { readonly points: readonly HistoryPoint[] }

const LazyScoreChart = lazy(async () => {
  const { CartesianGrid, Line, LineChart, ResponsiveContainer, XAxis, YAxis } = await import('recharts')
  return {
    default: function ScoreChart({ points }: ScoreChartProps) {
      const chartData = points.map(({ month, value }) => ({ month, value }))
      return (
        <div className="macro-chart" aria-hidden="true">
          <ResponsiveContainer width="100%" height="100%">
            <LineChart data={chartData} margin={{ top: 16, right: 16, left: 0, bottom: 0 }}>
              <CartesianGrid stroke="var(--border)" strokeDasharray="3 3" />
              <XAxis dataKey="month" minTickGap={40} stroke="var(--text-secondary)" />
              <YAxis domain={[0, 100]} width={40} stroke="var(--text-secondary)" />
              <Line type="linear" dataKey="value" stroke="var(--accent)" strokeWidth={2} connectNulls={false} isAnimationActive={false} dot />
            </LineChart>
          </ResponsiveContainer>
        </div>
      )
    },
  }
})

export function ScoreChart({ points }: ScoreChartProps) {
  return <LazyScoreChart points={points} />
}
