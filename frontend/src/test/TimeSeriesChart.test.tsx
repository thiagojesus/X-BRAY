import { describe, it, expect } from 'vitest'
import { render, screen, fireEvent } from '@testing-library/react'
import { TimeSeriesChart, BarChartComponent, formatValue, filterByWindow, makeXTickFormatter, makeTooltipFormatter, buildXTicks, detectPeriodicity, makeAdaptiveTickFormatter } from '../charts/TimeSeriesChart'

describe('formatValue', () => {
  it('formats pct', () => { expect(formatValue(5.5, 'pct')).toBe('5.50%') })
  it('formats brl', () => { expect(formatValue(1234.56, 'brl')).toContain('1.234,56') })
  it('formats usd', () => { expect(formatValue(5.2, 'usd')).toContain('5.20') })
  it('formats idx (default)', () => { expect(formatValue(100)).toContain('100,00') })
})

describe('makeXTickFormatter', () => {
  it('formats year axis - extracts year from DD/MM/YYYY', () => {
    const fmt = makeXTickFormatter('year')
    expect(fmt('15/06/2025')).toBe('2025')
  })
  it('returns raw value for non-date string', () => {
    const fmt = makeXTickFormatter('year')
    expect(fmt('invalid')).toBe('invalid')
  })
  it('formats date axis - shortens DD/MM/YYYY to MM/AA', () => {
    const fmt = makeXTickFormatter('date')
    expect(fmt('15/06/2025')).toBe('06/25')
  })
  it('formats ISO date axis', () => {
    const fmt = makeXTickFormatter('date')
    expect(fmt('2025-01-01')).toBe('01/25')
  })
  it('returns raw value for non-date string', () => {
    const fmt = makeXTickFormatter('date')
    expect(fmt('invalid')).toBe('invalid')
  })
})

describe('makeTooltipFormatter', () => {
  it('formats numeric value with pct format', () => {
    const map = new Map([['IPCA', 'pct' as const]])
    const fmt = makeTooltipFormatter(map)
    const result = fmt('5.5', 'IPCA')
    expect(result[0]).toBe('5.50%')
    expect(result[1]).toBe('IPCA')
  })
  it('returns raw for non-numeric value', () => {
    const map = new Map()
    const fmt = makeTooltipFormatter(map)
    const result = fmt('abc', 'Test')
    expect(result).toEqual(['abc', 'Test'])
  })
})

describe('filterByWindow', () => {
  const data = [
    { date: '01/01/2024', value: 1 },
    { date: '01/06/2024', value: 2 },
    { date: '01/01/2025', value: 3 },
    { date: '01/07/2025', value: 4 },
  ]

  it('returns empty array for empty data', () => {
    expect(filterByWindow([], 3)).toEqual([])
  })

  it('returns all data for Infinity months', () => {
    expect(filterByWindow(data, Infinity)).toEqual(data)
  })

  it('returns all when no date field', () => {
    const noDate = [{ value: 1 }]
    expect(filterByWindow(noDate as any, 3)).toEqual(noDate)
  })

  it('filters by YTD (months=-1)', () => {
    const result = filterByWindow(data, -1)
    expect(result.length).toBeGreaterThanOrEqual(1)
    expect(result.every(d => d.date.includes('2025'))).toBe(true)
  })

  it('filters by N months', () => {
    const result = filterByWindow(data, 12)
    expect(result.length).toBeGreaterThanOrEqual(1)
  })

  it('handles YYYY-MM-DD format dates', () => {
    const isoData = [
      { date: '2024-01-01', value: 1 },
      { date: '2025-01-01', value: 2 },
    ]
    const result = filterByWindow(isoData, -1)
    expect(result.length).toBe(1)
  })
})

describe('detectPeriodicity', () => {
  it('detects annual data', () => {
    const dates = ['01/01/2020', '01/01/2021', '01/01/2022', '01/01/2023']
    expect(detectPeriodicity(dates)).toBe('year')
  })

  it('detects monthly data', () => {
    const dates = ['01/01/2024', '01/02/2024', '01/03/2024', '01/04/2024', '01/05/2024']
    expect(detectPeriodicity(dates)).toBe('month')
  })

  it('detects daily data', () => {
    const dates = ['01/01/2025', '02/01/2025', '03/01/2025', '05/01/2025', '06/01/2025']
    expect(detectPeriodicity(dates)).toBe('day')
  })

  it('defaults to month for single date', () => {
    expect(detectPeriodicity(['01/01/2025'])).toBe('month')
  })
})

describe('buildXTicks', () => {
  it('returns empty ticks for empty data', () => {
    expect(buildXTicks([])).toEqual({ ticks: [], period: 'month' })
  })

  it('builds annual ticks for annual data', () => {
    const rows = [
      { date: '01/01/2020', v: 1 },
      { date: '01/01/2021', v: 2 },
      { date: '01/01/2022', v: 3 },
      { date: '01/01/2023', v: 4 },
    ]
    const { ticks, period } = buildXTicks(rows)
    expect(period).toBe('year')
    expect(ticks).toHaveLength(4)
    expect(ticks[0]).toBe('01/01/2020')
  })

  it('forces year ticks with forceYear', () => {
    const rows = [
      { date: '01/01/2020', v: 1 },
      { date: '01/06/2020', v: 2 },
      { date: '01/01/2021', v: 3 },
      { date: '01/06/2021', v: 4 },
    ]
    const { ticks, period } = buildXTicks(rows, { forceYear: true })
    expect(period).toBe('year')
    expect(ticks).toHaveLength(2)
  })

  it('builds monthly ticks for monthly data', () => {
    const rows = [
      { date: '01/01/2024', v: 1 },
      { date: '01/02/2024', v: 2 },
      { date: '01/03/2024', v: 3 },
    ]
    const { ticks, period } = buildXTicks(rows)
    expect(period).toBe('month')
    expect(ticks).toHaveLength(3)
  })

  it('limits ticks to 12 max', () => {
    const rows = Array.from({ length: 40 }, (_, i) => ({ date: `01/${String((i % 12) + 1).padStart(2, '0')}/${2020 + Math.floor(i / 12)}`, v: i }))
    const { ticks } = buildXTicks(rows)
    expect(ticks.length).toBeLessThanOrEqual(12)
  })

  it('builds day ticks for short daily span', () => {
    const rows = [
      { date: '01/08/2025', v: 1 },
      { date: '02/08/2025', v: 2 },
      { date: '05/08/2025', v: 3 },
      { date: '08/08/2025', v: 4 },
      { date: '12/08/2025', v: 5 },
      { date: '15/08/2025', v: 6 },
    ]
    const { ticks, period } = buildXTicks(rows)
    expect(period).toBe('day')
    expect(ticks.length).toBeGreaterThan(0)
  })

  it('ignores rows without valid dates', () => {
    const rows = [
      { date: 'invalid', v: 1 },
      { value: 2 },
    ]
    expect(buildXTicks(rows)).toEqual({ ticks: [], period: 'month' })
  })
})

describe('makeAdaptiveTickFormatter', () => {
  it('formats year period', () => {
    const fmt = makeAdaptiveTickFormatter('year')
    expect(fmt('15/06/2025')).toBe('2025')
  })

  it('formats month period as MM/AA', () => {
    const fmt = makeAdaptiveTickFormatter('month')
    expect(fmt('15/06/2025')).toBe('06/25')
  })

  it('formats day period as DD/MM', () => {
    const fmt = makeAdaptiveTickFormatter('day')
    expect(fmt('15/06/2025')).toBe('15/06')
  })

  it('returns raw value for non-date string', () => {
    const fmt = makeAdaptiveTickFormatter('month')
    expect(fmt('invalid')).toBe('invalid')
  })
})

const sampleData = [
  { date: '01/01/2025', value: 10 },
  { date: '01/02/2025', value: 12 },
  { date: '01/03/2025', value: 11 },
  { date: '01/04/2025', value: 15 },
]
const sampleSeries = [{ key: 'value', name: 'Teste', color: '#ff0000' }]

describe('TimeSeriesChart', () => {
  it('renders empty state when no data', () => {
    render(<TimeSeriesChart data={[]} series={sampleSeries} />)
    expect(screen.getByText('Sem dados disponíveis')).toBeInTheDocument()
  })

  it('renders chart title', () => {
    render(<TimeSeriesChart data={sampleData} series={sampleSeries} title="Minha Série" />)
    expect(screen.getByText('Minha Série')).toBeInTheDocument()
  })

  it('renders all window buttons', () => {
    render(<TimeSeriesChart data={sampleData} series={sampleSeries} />)
    expect(screen.getByText('1M')).toBeInTheDocument()
    expect(screen.getByText('3M')).toBeInTheDocument()
    expect(screen.getByText('6M')).toBeInTheDocument()
    expect(screen.getByText('YTD')).toBeInTheDocument()
    expect(screen.getByText('1Y')).toBeInTheDocument()
    expect(screen.getByText('5Y')).toBeInTheDocument()
    expect(screen.getByText('ALL')).toBeInTheDocument()
  })

  it('defaults to YTD window', () => {
    render(<TimeSeriesChart data={sampleData} series={sampleSeries} defaultWindow="YTD" />)
    const ytdBtn = screen.getByText('YTD')
    expect(ytdBtn.className).toContain('active')
  })

  it('switches window on click', () => {
    render(<TimeSeriesChart data={sampleData} series={sampleSeries} defaultWindow="YTD" />)
    fireEvent.click(screen.getByText('ALL'))
    expect(screen.getByText('ALL').className).toContain('active')
    expect(screen.getByText('YTD').className).not.toContain('active')
  })

  it('renders the chart container with data', () => {
    const { container } = render(<TimeSeriesChart data={sampleData} series={sampleSeries} />)
    expect(container.querySelector('.chart-container')).toBeInTheDocument()
  })

  it('renders with year xAxisFormat', () => {
    const yearData = [
      { date: '01/01/2023', value: 100 },
      { date: '01/01/2024', value: 200 },
    ]
    const { container } = render(<TimeSeriesChart data={yearData} series={sampleSeries} xAxisFormat="year" />)
    expect(container.querySelector('.chart-container')).toBeInTheDocument()
  })

  it('handles pct format', () => {
    const s = [{ key: 'val', name: 'Taxa', color: '#000', format: 'pct' as const }]
    const { container } = render(<TimeSeriesChart data={[{ date: '01/01/2025', val: 5.5 }]} series={s} />)
    expect(container.querySelector('.chart-container')).toBeInTheDocument()
  })

  it('handles brl format', () => {
    const s = [{ key: 'val', name: 'Preço', color: '#000', format: 'brl' as const }]
    const { container } = render(<TimeSeriesChart data={[{ date: '01/01/2025', val: 1234.56 }]} series={s} />)
    expect(container.querySelector('.chart-container')).toBeInTheDocument()
  })

  it('handles usd format', () => {
    const s = [{ key: 'val', name: 'Dólar', color: '#000', format: 'usd' as const }]
    const { container } = render(<TimeSeriesChart data={[{ date: '01/01/2025', val: 5.2 }]} series={s} />)
    expect(container.querySelector('.chart-container')).toBeInTheDocument()
  })

  it('handles dual Y-axis', () => {
    const s = [
      { key: 'a', name: 'A', color: '#000' },
      { key: 'b', name: 'B', color: '#fff', yAxisId: 'right' },
    ]
    const { container } = render(<TimeSeriesChart data={[{ date: '01/01/2025', a: 1, b: 2 }]} series={s} />)
    expect(container.querySelector('.chart-container')).toBeInTheDocument()
  })

  it('handles DD/MM/YYYY dates correctly', () => {
    const data = [
      { date: '01/01/2025', value: 1 },
      { date: '15/06/2025', value: 2 },
      { date: '01/08/2025', value: 3 },
    ]
    const { container } = render(<TimeSeriesChart data={data} series={sampleSeries} defaultWindow="ALL" />)
    expect(container.querySelector('.chart-container')).toBeInTheDocument()
  })

  it('handles YYYY-MM-DD dates in parseDate', () => {
    const data = [
      { date: '2025-01-01', value: 1 },
      { date: '2025-06-15', value: 2 },
    ]
    const { container } = render(<TimeSeriesChart data={data} series={sampleSeries} defaultWindow="ALL" />)
    expect(container.querySelector('.chart-container')).toBeInTheDocument()
  })

  it('renders window buttons with active state', () => {
    render(<TimeSeriesChart data={sampleData} series={sampleSeries} defaultWindow="1Y" />)
    expect(screen.getByText('1Y').className).toContain('active')
    fireEvent.click(screen.getByText('3M'))
    expect(screen.getByText('3M').className).toContain('active')
    expect(screen.getByText('1Y').className).not.toContain('active')
  })
})

describe('BarChartComponent', () => {
  it('renders empty state when no data', () => {
    render(<BarChartComponent data={[]} series={sampleSeries} />)
    expect(screen.getByText('Sem dados disponíveis')).toBeInTheDocument()
  })

  it('renders chart with data', () => {
    const { container } = render(<BarChartComponent data={sampleData} series={sampleSeries} title="Gráfico de Barras" />)
    expect(screen.getByText('Gráfico de Barras')).toBeInTheDocument()
    expect(container.querySelector('.chart-container')).toBeInTheDocument()
  })

  it('renders without title', () => {
    const { container } = render(<BarChartComponent data={sampleData} series={sampleSeries} />)
    expect(container.querySelector('.chart-container')).toBeInTheDocument()
  })
})
