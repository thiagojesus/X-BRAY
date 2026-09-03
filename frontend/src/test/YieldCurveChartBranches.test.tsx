import { fireEvent, render, screen } from '@testing-library/react'
import { describe, expect, it } from 'vitest'

import { YieldCurveChart, type CurvasDiData } from '../charts/YieldCurveChart'

const curveData = {
  source: 'B3',
  days: 4,
  dates: ['11/08/2026', '12/08/2026', '13/08/2026', '14/08/2026'],
  curves: {
    '11/08/2026': [{ symbol: 'DI1F27', maturity: '2027-01-04', rate: 13.1 }],
    '12/08/2026': [{ symbol: 'DI1F27', maturity: '2027-01-04', rate: 13.0 }],
    '13/08/2026': [{ symbol: 'DI1F27', maturity: '2027-01-04', rate: 12.9 }],
    '14/08/2026': [{ symbol: 'DI1F27', maturity: '2027-01-04', rate: 12.8 }],
  },
} satisfies CurvasDiData

describe('YieldCurveChart custom ranges', () => {
  it('shows and clears custom date controls', () => {
    // Given
    render(<YieldCurveChart data={curveData} />)
    const initialDate = screen.getByLabelText('Data inicial')
    const finalDate = screen.getByLabelText('Data final')

    // When
    fireEvent.change(initialDate, { target: { value: '2026-08-12' } })
    fireEvent.change(finalDate, { target: { value: '2026-08-13' } })

    // Then
    expect(screen.getByRole('button', { name: 'Limpar' })).toBeInTheDocument()
    expect(screen.getByRole('button', { name: '5 dias' })).not.toHaveClass('active')

    // When
    fireEvent.click(screen.getByRole('button', { name: 'Limpar' }))

    // Then
    expect(screen.queryByRole('button', { name: 'Limpar' })).not.toBeInTheDocument()
    expect(screen.getByRole('button', { name: '5 dias' })).toHaveClass('active')
  })

  it('supports a final-date-only selection', () => {
    // Given
    render(<YieldCurveChart data={curveData} />)

    // When
    fireEvent.change(screen.getByLabelText('Data final'), { target: { value: '2026-08-13' } })

    // Then
    expect(screen.getByRole('button', { name: 'Limpar' })).toBeInTheDocument()
    expect(screen.getByLabelText('Data inicial')).toHaveValue('')
  })

  it('restores quick-range mode when a day button is selected', () => {
    // Given
    render(<YieldCurveChart data={curveData} />)
    fireEvent.change(screen.getByLabelText('Data inicial'), { target: { value: '2026-08-12' } })

    // When
    fireEvent.click(screen.getByRole('button', { name: '10 dias' }))

    // Then
    expect(screen.queryByRole('button', { name: 'Limpar' })).not.toBeInTheDocument()
    expect(screen.getByRole('button', { name: '10 dias' })).toHaveClass('active')
  })

  it('renders sparse curves and null rates without failing', () => {
    // Given
    const sparseData = {
      source: 'B3',
      days: 2,
      dates: ['13/08/2026', '14/08/2026'],
      curves: {
        '13/08/2026': [{ symbol: 'DI1F27', maturity: '2027-01-04', rate: null }],
      },
    } satisfies CurvasDiData

    // When
    const { container } = render(<YieldCurveChart data={sparseData} />)

    // Then
    expect(container.querySelector('[data-testid="recharts-container"]')).toBeInTheDocument()
    expect(screen.queryByText('Sem dados disponíveis')).not.toBeInTheDocument()
  })

  it('renders a chart shell when every selected curve is empty', () => {
    // Given
    const emptyCurves = {
      source: 'B3',
      days: 1,
      dates: ['14/08/2026'],
      curves: {},
    } satisfies CurvasDiData

    // When
    const { container } = render(<YieldCurveChart data={emptyCurves} />)

    // Then
    expect(container.querySelector('[data-testid="responsive-container"]')).toBeInTheDocument()
  })
})
