import { describe, it, expect } from 'vitest'
import { render, screen } from '@testing-library/react'
import { YieldCurveChart } from '../charts/YieldCurveChart'

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
    render(<YieldCurveChart data={data} />)
    expect(screen.getByLabelText('Data inicial')).toBeInTheDocument()
    expect(screen.getByLabelText('Data final')).toBeInTheDocument()
  })

  it('passes maturity rows and date series to the line chart', () => {
    const { container } = render(<YieldCurveChart data={data} />)
    const chart = container.querySelector('[data-testid="recharts-container"]')
    expect(chart).not.toBeNull()
    const props = JSON.parse(chart!.getAttribute('data-props') || '[]')
    expect(props).toContain('data')
  })

  it('renders the chart container', () => {
    render(<YieldCurveChart data={data} />)
    expect(screen.getByText('Curvas de Juros Futuros (DI)')).toBeInTheDocument()
    expect(document.querySelector('.chart-container')).toBeInTheDocument()
  })
})
