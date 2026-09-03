import { render, screen } from '@testing-library/react'
import { describe, expect, it } from 'vitest'

import { TimeSeriesChart, buildXTicks } from '../charts/TimeSeriesChart'

function dailyRows(total: number) {
  return Array.from({ length: total }, (_, index) => {
    const date = new Date(Date.UTC(2025, 0, index + 1))
    return { date: date.toISOString().slice(0, 10), value: index }
  })
}

describe('buildXTicks daily spans', () => {
  it('samples weekly ticks for a medium daily span', () => {
    // Given
    const rows = dailyRows(80)

    // When
    const result = buildXTicks(rows)

    // Then
    expect(result.period).toBe('day')
    expect(result.ticks.length).toBeLessThanOrEqual(12)
  })

  it('uses monthly ticks for a long daily span', () => {
    // Given
    const rows = dailyRows(300)

    // When
    const result = buildXTicks(rows)

    // Then
    expect(result.period).toBe('month')
    expect(result.ticks.length).toBeLessThanOrEqual(12)
  })

  it('uses yearly ticks when daily data spans more than 400 days', () => {
    // Given
    const rows = dailyRows(500)

    // When
    const result = buildXTicks(rows)

    // Then
    expect(result.period).toBe('year')
    expect(result.ticks).toEqual(['2025-01-01', '2026-01-01'])
  })
})

describe('TimeSeriesChart fallback configuration', () => {
  it('uses the full dataset for an unknown default window', () => {
    // Given
    const rows = dailyRows(2)
    const series = [{ key: 'value', name: 'Value', color: '#fff' }]

    // When
    render(<TimeSeriesChart data={rows} series={series} defaultWindow="UNKNOWN" />)

    // Then
    expect(screen.getByRole('button', { name: 'ALL' })).toBeInTheDocument()
    expect(screen.queryByRole('heading')).not.toBeInTheDocument()
    expect(screen.queryByText('Sem dados disponíveis')).not.toBeInTheDocument()
  })
})
