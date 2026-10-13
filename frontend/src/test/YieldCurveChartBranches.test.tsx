import { fireEvent, render, screen } from '@testing-library/react'
import type { ReactNode } from 'react'
import { describe, expect, it, vi } from 'vitest'

import { type CurvasDiData, YieldCurveChart } from '../charts/YieldCurveChart'

vi.mock('recharts', () => ({
  ResponsiveContainer: ({ children }: { readonly children?: ReactNode }) => <div data-testid="responsive-container">{children}</div>,
  LineChart: ({ children }: { readonly children?: ReactNode }) => <div data-testid="recharts-container">{children}</div>,
  Line: ({ name }: { readonly name: string }) => <span data-testid="curve-date">{name}</span>,
  XAxis: () => null,
  YAxis: () => null,
  CartesianGrid: () => null,
  Tooltip: () => null,
  Legend: () => null,
}))

const dates = Array.from({ length: 12 }, (_, index) => `${String(index + 3).padStart(2, '0')}/08/2026`)
const curveData = {
  source: 'B3',
  days: 12,
  dates,
  curves: Object.fromEntries(dates.map(date => [date, [{ symbol: 'DI1F27', maturity: '2027-01-04', rate: 13.1 }]])),
} satisfies CurvasDiData

const selectedDates = () => screen.queryAllByTestId('curve-date').map(element => element.textContent)

describe('YieldCurveChart custom ranges', () => {
  it('includes both boundaries when pt-BR dates are entered', () => {
    // Given
    render(<YieldCurveChart data={curveData} />)
    const initialDate = screen.getByLabelText('Data inicial')
    const finalDate = screen.getByLabelText('Data final')

    // When
    fireEvent.change(initialDate, { target: { value: '12/08/2026' } })
    fireEvent.change(finalDate, { target: { value: '13/08/2026' } })

    // Then
    expect(selectedDates()).toEqual(['12/08/2026', '13/08/2026'])
    expect(initialDate).toHaveValue('12/08/2026')
    expect(finalDate).toHaveValue('13/08/2026')
    expect(screen.getByRole('button', { name: 'Limpar' })).toBeInTheDocument()
    expect(screen.getByRole('button', { name: '5 dias' })).not.toHaveClass('active')
  })

  it('restores the last five curves when a custom range is cleared', () => {
    // Given
    render(<YieldCurveChart data={curveData} />)
    fireEvent.change(screen.getByLabelText('Data inicial'), { target: { value: '12/08/2026' } })
    fireEvent.change(screen.getByLabelText('Data final'), { target: { value: '13/08/2026' } })

    // When
    fireEvent.click(screen.getByRole('button', { name: 'Limpar' }))

    // Then
    expect(selectedDates()).toEqual(dates.slice(-5))
    expect(screen.getByLabelText('Data inicial')).toHaveValue('')
    expect(screen.getByLabelText('Data final')).toHaveValue('')
    expect(screen.queryByRole('button', { name: 'Limpar' })).not.toBeInTheDocument()
    expect(screen.getByRole('button', { name: '5 dias' })).toHaveClass('active')
  })

  it('supports a final-date-only selection', () => {
    // Given
    render(<YieldCurveChart data={curveData} />)

    // When
    fireEvent.change(screen.getByLabelText('Data final'), { target: { value: '13/08/2026' } })

    // Then
    expect(selectedDates()).toEqual(dates.slice(0, -1))
    expect(screen.getByRole('button', { name: 'Limpar' })).toBeInTheDocument()
    expect(screen.getByLabelText('Data inicial')).toHaveValue('')
  })

  it('keeps all dates at or after a start-only boundary', () => {
    // Given
    render(<YieldCurveChart data={curveData} />)

    // When
    fireEvent.change(screen.getByLabelText('Data inicial'), { target: { value: '12/08/2026' } })

    // Then
    expect(selectedDates()).toEqual(['12/08/2026', '13/08/2026', '14/08/2026'])
    expect(screen.getByLabelText('Data final')).toHaveValue('')
  })

  it.each([5, 10, 15, 30])('restores the last %i curves when a day button is selected', range => {
    // Given
    render(<YieldCurveChart data={curveData} />)
    fireEvent.change(screen.getByLabelText('Data inicial'), { target: { value: '12/08/2026' } })
    fireEvent.change(screen.getByLabelText('Data final'), { target: { value: '13/08/2026' } })

    // When
    fireEvent.click(screen.getByRole('button', { name: `${range} dias` }))

    // Then
    expect(selectedDates()).toEqual(dates.slice(-range))
    expect(screen.getByLabelText('Data inicial')).toHaveValue('')
    expect(screen.getByLabelText('Data final')).toHaveValue('')
    expect(screen.queryByRole('button', { name: 'Limpar' })).not.toBeInTheDocument()
    expect(screen.getByRole('button', { name: `${range} dias` })).toHaveClass('active')
  })

  it.each([
    ['Data inicial', '31/04/2026'], ['Data final', '29/02/2026'],
    ['Data inicial', '32/08/2026'], ['Data inicial', '00/08/2026'],
    ['Data inicial', '12/00/2026'], ['Data inicial', '12/13/2026'],
    ['Data inicial', '12/08/26'], ['Data inicial', '12/08'],
    ['Data inicial', '2026-08-12'], ['Data inicial', '08/13/2026'],
  ])('rejects %s = %s instead of treating it as a valid boundary', (label, value) => {
    // Given
    render(<YieldCurveChart data={curveData} />)
    const input = screen.getByLabelText(label)

    // When
    fireEvent.change(input, { target: { value } })

    // Then
    expect(input).toHaveAttribute('aria-invalid', 'true')
    expect(selectedDates()).toEqual([])
    const alert = screen.getByRole('alert')
    expect(alert).toBeVisible()
    expect(alert).toHaveClass('chart-series-unavailable')
    expect(alert).not.toHaveClass('chart-title')
    expect(alert.id).not.toBe('')
    expect(input).toHaveAttribute('aria-describedby', alert.id)
    expect(input).toHaveAccessibleDescription(alert.textContent ?? '')
  })

  it('keeps the alert association stable when the second input becomes invalid', () => {
    // Given
    render(<YieldCurveChart data={curveData} />)
    fireEvent.change(screen.getByLabelText('Data inicial'), { target: { value: '31/04/2026' } })
    const alertId = screen.getByRole('alert').id

    // When
    fireEvent.change(screen.getByLabelText('Data final'), { target: { value: '29/02/2026' } })

    // Then
    expect(screen.getByRole('alert')).toHaveAttribute('id', alertId)
    expect(screen.getByLabelText('Data inicial')).toHaveAttribute('aria-describedby', alertId)
    expect(screen.getByLabelText('Data final')).toHaveAttribute('aria-describedby', alertId)
  })

  it('removes the alert association when the invalid date is corrected', () => {
    // Given
    render(<YieldCurveChart data={curveData} />)
    fireEvent.change(screen.getByLabelText('Data inicial'), { target: { value: '31/04/2026' } })

    // When
    fireEvent.change(screen.getByLabelText('Data inicial'), { target: { value: '12/08/2026' } })

    // Then
    expect(screen.queryByRole('alert')).not.toBeInTheDocument()
    expect(screen.getByLabelText('Data inicial')).not.toHaveAttribute('aria-describedby')
    expect(screen.getByLabelText('Data inicial')).toHaveAttribute('aria-invalid', 'false')
    expect(selectedDates()).toEqual(['12/08/2026', '13/08/2026', '14/08/2026'])
  })

  it.each([
    ['31/12/2025', '01/01/2026', ['31/12/2025', '01/01/2026']],
    ['29/02/2024', '29/02/2024', ['29/02/2024']],
    ['02/01/2026', '', []],
    ['01/01/2026', '31/12/2025', []],
  ])('selects chronological dates when the range is %s to %s', (from, to, expected) => {
    // Given
    const calendarData = { ...curveData, dates: ['29/02/2024', '31/12/2025', '01/01/2026'] }
    render(<YieldCurveChart data={calendarData} />)

    // When
    fireEvent.change(screen.getByLabelText('Data inicial'), { target: { value: from } })
    fireEvent.change(screen.getByLabelText('Data final'), { target: { value: to } })

    // Then
    expect(selectedDates()).toEqual(expected)
    expect(screen.getByLabelText('Data inicial')).toHaveAttribute('aria-invalid', 'false')
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
