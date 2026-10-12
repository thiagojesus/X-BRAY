import { useState, useMemo } from 'react'
import { LineChart, Line, BarChart, Bar, XAxis, YAxis, CartesianGrid, Tooltip, Legend, ResponsiveContainer } from 'recharts'
import { buildXTicks, filterByWindow, makeAdaptiveTickFormatter, makeTooltipFormatter } from './timeSeriesUtils'
import type { SeriesFormat } from './timeSeriesUtils'
import { normalizeSeriesBase100 } from '../utils/series'

export {
  buildXTicks,
  detectPeriodicity,
  filterByWindow,
  formatValue,
  makeAdaptiveTickFormatter,
  makeTooltipFormatter,
  makeXTickFormatter,
} from './timeSeriesUtils'
export type { SeriesFormat, TickPeriod } from './timeSeriesUtils'

export interface SeriesConfig {
  key: string
  name: string
  color: string
  yAxisId?: string
  format?: SeriesFormat
}

interface TimeSeriesChartProps {
  data: object[]
  series: readonly SeriesConfig[]
  title?: string
  yLabel?: string
  rightYLabel?: string
  height?: number
  defaultWindow?: string
  xAxisFormat?: 'date' | 'year'
  yTickFormatter?: (value: number) => string
  rightYTickFormatter?: (value: number) => string
  connectNulls?: boolean
  showReferenceDates?: boolean
  normalizeKeys?: readonly string[]
}

type AxisLabelViewBox = {
  readonly x?: number
  readonly y?: number
  readonly width?: number
  readonly height?: number
}

type AxisLabelContentProps = {
  readonly viewBox?: AxisLabelViewBox
  readonly value?: number | string
}

export function VerticalAxisLabel({
  side,
  value,
  viewBox,
}: AxisLabelContentProps & { readonly side: 'left' | 'right' }) {
  if (
    value === undefined
    || viewBox?.x === undefined
    || viewBox.y === undefined
    || viewBox.width === undefined
    || viewBox.height === undefined
  ) {
    return null
  }

  const x = side === 'left' ? viewBox.x + 5 : viewBox.x + viewBox.width - 5
  const y = viewBox.y + viewBox.height / 2
  const angle = side === 'left' ? -90 : 90
  return (
    <text
      className="recharts-text recharts-label"
      x={x}
      y={y}
      textAnchor="middle"
      fill="#999"
      fontSize={11}
      transform={`rotate(${angle}, ${x}, ${y})`}
    >
      <tspan x={x} dy="0.355em">{value}</tspan>
    </text>
  )
}

const renderLeftAxisLabel = (props: AxisLabelContentProps) => (
  <VerticalAxisLabel {...props} side="left" />
)

const renderRightAxisLabel = (props: AxisLabelContentProps) => (
  <VerticalAxisLabel {...props} side="right" />
)

const WINDOWS = [
  { key: '1M', months: 1 },
  { key: '3M', months: 3 },
  { key: '6M', months: 6 },
  { key: 'YTD', months: -1 },
  { key: '1Y', months: 12 },
  { key: '5Y', months: 60 },
  { key: 'ALL', months: Infinity },
]

export function TimeSeriesChart({
  data,
  series,
  title,
  yLabel,
  rightYLabel,
  height = 350,
  defaultWindow = 'YTD',
  xAxisFormat = 'date',
  yTickFormatter,
  rightYTickFormatter,
  connectNulls = true,
  showReferenceDates = false,
  normalizeKeys = [],
}: TimeSeriesChartProps) {
  const [windowKey, setWindowKey] = useState(defaultWindow)
  const months = WINDOWS.find(w => w.key === windowKey)?.months ?? Infinity

  const filtered = useMemo(() => filterByWindow(data, months), [data, months])
  const normalized = useMemo(
    () => normalizeKeys.length > 0
      ? normalizeSeriesBase100(filtered, normalizeKeys)
      : { data: filtered, unavailable: [] },
    [filtered, normalizeKeys],
  )
  const chartData = normalized.data

  const { ticks, period } = useMemo(
    () => buildXTicks(chartData, { forceYear: xAxisFormat === 'year' }),
    [chartData, xAxisFormat]
  )

  if (!data || data.length === 0) {
    return <div className="chart-empty">Sem dados disponíveis</div>
  }

  const hasRightAxis = series.some(s => s.yAxisId === 'right')
  const formatMap = new Map(series.map(s => [s.name, s.format]))

  const tooltipFormatter = makeTooltipFormatter(formatMap, showReferenceDates)

  const tooltipLabelFormatter = (label: string) => label

  const xTickFormatter = makeAdaptiveTickFormatter(period)

  return (
    <div className="chart-container">
      <div className="chart-header">
        {title && <h3 className="chart-title">{title}</h3>}
        <div className="time-window">
          {WINDOWS.map(w => (
            <button
              type="button"
              key={w.key}
              className={`tw-btn ${windowKey === w.key ? 'active' : ''}`}
              onClick={() => setWindowKey(w.key)}
            >
              {w.key}
            </button>
          ))}
        </div>
      </div>
      {normalized.unavailable.length > 0 && (
        <p className="chart-series-unavailable">
          Sem base válida na janela: {series
            .filter(item => normalized.unavailable.includes(item.key))
            .map(item => item.name)
            .join(', ')}.
        </p>
      )}
      <ResponsiveContainer width="100%" height={height}>
        <LineChart data={[...chartData]} margin={{ top: 5, right: 30, left: 20, bottom: 5 }}>
          <CartesianGrid strokeDasharray="3 3" stroke="#333" />
          <XAxis
            dataKey="date"
            tick={{ fontSize: 11, fill: '#999' }}
            ticks={ticks}
            tickFormatter={xTickFormatter}
          />
          <YAxis yAxisId="left" tick={{ fontSize: 11, fill: '#999' }} tickFormatter={yTickFormatter} label={yLabel ? { value: yLabel, content: renderLeftAxisLabel } : undefined} />
          {hasRightAxis && (
            <YAxis
              yAxisId="right"
              orientation="right"
              tick={{ fontSize: 11, fill: '#999' }}
              tickFormatter={rightYTickFormatter}
              label={rightYLabel ? { value: rightYLabel, content: renderRightAxisLabel } : undefined}
            />
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
              connectNulls={connectNulls}
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
  const { ticks, period } = useMemo(() => buildXTicks(data), [data])

  if (data.length === 0) {
    return <div className="chart-empty">Sem dados disponíveis</div>
  }

  const formatMap = new Map(series.map(s => [s.name, s.format]))

  const tooltipFormatter = makeTooltipFormatter(formatMap)

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
