import { fireEvent, render, screen } from '@testing-library/react'
import type { ReactNode } from 'react'
import { describe, expect, it, vi } from 'vitest'
import { type CurvasDiData, YieldCurveChart } from '../charts/YieldCurveChart'

vi.mock('recharts', () => ({
  ResponsiveContainer: ({ children }: { readonly children?: ReactNode }) => <div data-testid="responsive-container">{children}</div>,
  LineChart: ({ children, ...props }: { readonly children?: ReactNode; readonly data: readonly object[] }) => (
    <div data-testid="recharts-container" data-props={JSON.stringify(Object.keys(props))}>
      <output data-testid="curve-values">{JSON.stringify(props.data)}</output>
      {children}
    </div>
  ),
  Line: () => null,
  XAxis: () => null,
  YAxis: ({ domain, ticks = [], tickFormatter }: {
    readonly domain?: readonly (number | string)[]
    readonly ticks?: readonly number[]
    readonly tickFormatter?: (value: number, index: number) => string
  }) => (
    <div data-testid="di-axis" data-domain={JSON.stringify(domain)} data-ticks={JSON.stringify(ticks)}>
      {ticks.map((tick, index) => <span key={tick} data-testid="di-tick">{tickFormatter?.(tick, index) ?? String(tick)}</span>)}
    </div>
  ),
  CartesianGrid: () => null,
  Tooltip: () => null,
  Legend: () => null,
}))

const data = {
  source: 'B3 Price Report (SPR)',
  days: 30,
  dates: ['12/08/2026', '13/08/2026', '14/08/2026'],
  curves: {
    '12/08/2026': [
      { symbol: 'DI1U26', maturity: '2026-09-01', rate: 13.902 },
      { symbol: 'DI1V26', maturity: '2026-10-01', rate: 13.843 },
      { symbol: 'DI1X26', maturity: '2026-11-03', rate: 13.802 },
    ],
    '13/08/2026': [
      { symbol: 'DI1U26', maturity: '2026-09-01', rate: 13.905 },
      { symbol: 'DI1V26', maturity: '2026-10-01', rate: 13.85 },
      { symbol: 'DI1X26', maturity: '2026-11-03', rate: 13.8 },
    ],
    '14/08/2026': [
      { symbol: 'DI1U26', maturity: '2026-09-01', rate: 13.904 },
      { symbol: 'DI1V26', maturity: '2026-10-01', rate: 13.852 },
      { symbol: 'DI1X26', maturity: '2026-11-03', rate: 13.795 },
    ],
  },
}

describe('YieldCurveChart', () => {
  it('renders empty state when no data', () => {
    render(<YieldCurveChart data={null} />)
    expect(screen.getByText('Sem dados disponíveis')).toBeInTheDocument()
  })

  it('renders empty state when no dates', () => {
    render(<YieldCurveChart data={{ source: 'B3', days: 30, dates: [], curves: {} }} />)
    expect(screen.getByText('Sem dados disponíveis')).toBeInTheDocument()
  })

  it('renders title and day buttons', () => {
    render(<YieldCurveChart data={data} />)
    expect(screen.getByText('Curvas de Juros Futuros (DI)')).toBeInTheDocument()
    expect(screen.getByText('5 dias')).toBeInTheDocument()
    expect(screen.getByText('10 dias')).toBeInTheDocument()
    expect(screen.getByText('15 dias')).toBeInTheDocument()
    expect(screen.getByText('30 dias')).toBeInTheDocument()
  })

  it('renders date inputs for custom range', () => {
    // Given / When
    render(<YieldCurveChart data={data} />)

    // Then
    for (const label of ['Data inicial', 'Data final']) {
      expect(screen.getByLabelText(label)).toHaveAttribute('type', 'text')
      expect(screen.getByLabelText(label)).toHaveAttribute('placeholder', 'DD/MM/AAAA')
    }
  })

  it('passes maturity rows and date series to the line chart', () => {
    const { container } = render(<YieldCurveChart data={data} />)
    const chart = container.querySelector('[data-testid="recharts-container"]')
    expect(chart).not.toBeNull()
    expect(chart).toHaveAttribute('data-props', expect.stringContaining('"data"'))
  })

  it('renders the chart container', () => {
    render(<YieldCurveChart data={data} />)
    expect(screen.getByText('Curvas de Juros Futuros (DI)')).toBeInTheDocument()
    expect(document.querySelector('.chart-container')).toBeInTheDocument()
  })
})

describe('YieldCurveChart rate axis', () => {
  it('uses clean bounds and pt-BR labels when visible rates have a narrow spread', () => {
    // Given / When
    render(<YieldCurveChart data={data} />)

    // Then
    expect(screen.getByTestId('di-axis')).toHaveAttribute('data-domain', '[13.75,13.95]')
    expect(screen.getByTestId('di-axis')).toHaveAttribute('data-ticks', '[13.75,13.8,13.85,13.9,13.95]')
    expect(screen.getAllByTestId('di-tick').map(tick => tick.textContent)).toEqual(['13,75', '13,80', '13,85', '13,90', '13,95'])
  })

  it.each([
    { rates: [13.801, 13.808], domain: [13.8, 13.81], ticks: [13.8, 13.802, 13.804, 13.806, 13.808, 13.81], labels: ['13,800', '13,802', '13,804', '13,806', '13,808', '13,810'] },
    { rates: [2, 18], domain: [0, 20], ticks: [0, 5, 10, 15, 20], labels: ['0', '5', '10', '15', '20'] },
    { rates: [13.902, 13.902], domain: [13.7, 14.1], ticks: [13.7, 13.8, 13.9, 14, 14.1], labels: ['13,7', '13,8', '13,9', '14,0', '14,1'] },
    { rates: [0], domain: [-0.1, 0.1], ticks: [-0.1, -0.05, 0, 0.05, 0.1], labels: ['-0,10', '-0,05', '0,00', '0,05', '0,10'] },
    { rates: [0.1 + 0.2, 0.6 + 0.1], domain: [0.2, 0.8], ticks: [0.2, 0.3, 0.4, 0.5, 0.6, 0.7, 0.8], labels: ['0,2', '0,3', '0,4', '0,5', '0,6', '0,7', '0,8'] },
  ])('keeps bounds, ticks and precision sensible when rates are $rates', ({ rates, domain, ticks, labels }) => {
    // Given
    const fixture = {
      ...data,
      dates: ['14/08/2026'],
      curves: { '14/08/2026': rates.map((rate, index) => ({ symbol: `DI1${index}`, maturity: `${2027 + index}-01-04`, rate })) },
    } satisfies CurvasDiData

    // When
    render(<YieldCurveChart data={fixture} />)

    // Then
    expect(screen.getByTestId('di-axis')).toHaveAttribute('data-domain', JSON.stringify(domain))
    expect(screen.getByTestId('di-axis')).toHaveAttribute('data-ticks', JSON.stringify(ticks))
    expect(screen.getAllByTestId('di-tick').map(tick => tick.textContent)).toEqual(labels)
  })

  it.each([
    { state: 'empty', rates: [] },
    { state: 'all-null', rates: [null, null] },
    { state: 'non-finite', rates: [Number.NaN, Infinity, -Infinity] },
    { state: 'mixed missing', rates: [null, Number.NaN, Infinity, -Infinity] },
  ])('uses an automatic domain without numeric ticks when visible rates are $state', ({ rates }) => {
    // Given
    const fixture = {
      ...data,
      dates: ['14/08/2026'],
      curves: { '14/08/2026': rates.map((rate, index) => ({ symbol: `DI1${index}`, maturity: `${2027 + index}-01-04`, rate })) },
    } satisfies CurvasDiData

    // When
    render(<YieldCurveChart data={fixture} />)

    // Then
    expect(screen.getByTestId('di-axis')).toHaveAttribute('data-domain', '["auto","auto"]')
    expect(screen.getByTestId('di-axis')).toHaveAttribute('data-ticks', '[]')
    expect(screen.queryAllByTestId('di-tick')).toEqual([])
  })

  it('uses an automatic domain when filtering leaves only missing rates visible', () => {
    // Given
    const fixture = {
      ...data,
      curves: { ...data.curves, '14/08/2026': [{ symbol: 'DI1U26', maturity: '2026-09-01', rate: null }] },
    } satisfies CurvasDiData
    render(<YieldCurveChart data={fixture} />)

    // When
    fireEvent.change(screen.getByLabelText('Data inicial'), { target: { value: '14/08/2026' } })

    // Then
    expect(screen.getByTestId('di-axis')).toHaveAttribute('data-domain', '["auto","auto"]')
    expect(screen.getByTestId('di-axis')).toHaveAttribute('data-ticks', '[]')
    expect(screen.queryAllByTestId('di-tick')).toEqual([])
  })

  it('ignores hidden outliers and non-finite rates when the visible dates change', () => {
    // Given
    const fixture = {
      ...data,
      curves: {
        ...data.curves,
        '12/08/2026': [{ symbol: 'DI1F27', maturity: '2027-01-04', rate: 1000 }],
        '14/08/2026': [
          ...data.curves['14/08/2026'],
          ...[null, Number.NaN, Infinity, -Infinity].map((rate, index) => ({ symbol: `DI1${index}`, maturity: `${2028 + index}-01-04`, rate })),
        ],
      },
    } satisfies CurvasDiData
    render(<YieldCurveChart data={fixture} />)

    // When
    fireEvent.change(screen.getByLabelText('Data inicial'), { target: { value: '13/08/2026' } })

    // Then
    expect(screen.getByTestId('di-axis')).toHaveAttribute('data-domain', '[13.75,13.95]')
    expect(screen.getByTestId('di-axis')).toHaveAttribute('data-ticks', '[13.75,13.8,13.85,13.9,13.95]')
  })

  it('preserves source date keys and unrounded values when creating chart rows', () => {
    // Given
    const source = JSON.stringify(data)

    // When
    render(<YieldCurveChart data={data} />)

    // Then
    expect(screen.getByTestId('curve-values').textContent).toBe(JSON.stringify([
      { maturity: '09/26', '12/08/2026': 13.902, '13/08/2026': 13.905, '14/08/2026': 13.904 },
      { maturity: '10/26', '12/08/2026': 13.843, '13/08/2026': 13.85, '14/08/2026': 13.852 },
      { maturity: '11/26', '12/08/2026': 13.802, '13/08/2026': 13.8, '14/08/2026': 13.795 },
    ]))
    expect(JSON.stringify(data)).toBe(source)
  })
})
