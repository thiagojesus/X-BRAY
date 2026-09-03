import { fireEvent, render, screen } from '@testing-library/react'
import { describe, expect, it } from 'vitest'

import BrasilMap, {
  computeUFsForDay,
  type StatesPayload,
  type UFData,
} from '../charts/BrasilMap'

const state = {
  uf: 'SP',
  candidates: [
    { name: 'Candidate A', price: 0.6, volume: 100 },
    { name: 'Candidate B', price: 0.4, volume: 50 },
  ],
  history: {
    '2026-09-01': { 'Candidate B': 55, 'Candidate A': 45 },
    '2026-09-02': { 'Candidate A': 60, 'Candidate B': 40 },
  },
} satisfies UFData

const payload = {
  source: 'Polymarket',
  updated_at: '2026-09-02T12:00:00Z',
  days: ['2026-09-01', '2026-09-02'],
  ufs: [state],
} satisfies StatesPayload

describe('BrasilMap branch behavior', () => {
  it('falls back for unknown states, empty history, and unmapped leaders', () => {
    // Given
    const states: UFData[] = [
      { uf: 'ZZ', candidates: [], history: { '2026-09-02': {} } },
      {
        uf: 'XY',
        candidates: [{ name: 'Candidate X', price: 0.5, volume: 1 }],
        history: { '2026-09-02': { 'Candidate X': 50 } },
      },
    ]

    // When
    const result = computeUFsForDay(states, '2026-09-02', new Map())

    // Then
    expect(result[0]).toMatchObject({ name: 'ZZ', leader: null, color: null })
    expect(result[1]).toMatchObject({ name: 'XY', leader: 'Candidate X', color: '#999', runnerUpPct: 0 })
  })

  it('renders an undated empty map when no days exist', () => {
    // Given
    const emptyPayload = {
      source: 'Polymarket',
      updated_at: '2026-09-02T12:00:00Z',
      days: [],
      ufs: [],
    } satisfies StatesPayload

    // When
    render(<BrasilMap payload={emptyPayload} />)

    // Then
    expect(screen.getByText('—')).toBeInTheDocument()
    expect(screen.getByText('Sem dados para este dia')).toBeInTheDocument()
  })

  it('moves the date slider to an earlier snapshot', () => {
    // Given
    render(<BrasilMap payload={payload} />)

    // When
    fireEvent.change(screen.getByRole('slider'), { target: { value: '0' } })

    // Then
    expect(screen.getByText('01/09/2026')).toBeInTheDocument()
  })

  it('shows and hides leader details while hovering a state', () => {
    // Given
    const { container } = render(<BrasilMap payload={payload} />)
    const statePath = container.querySelector('[data-uf="SP"]')
    expect(statePath).not.toBeNull()
    if (statePath === null) return

    // When
    fireEvent.mouseEnter(statePath)

    // Then
    expect(screen.getByText('São Paulo')).toBeInTheDocument()
    expect(screen.getByText(/Candidate A — 60.0%/)).toBeInTheDocument()

    // When
    fireEvent.mouseLeave(statePath)

    // Then
    expect(screen.queryByText('São Paulo')).not.toBeInTheDocument()
  })

  it('does not show leader details for a state without history', () => {
    // Given
    const noHistoryPayload = {
      ...payload,
      ufs: [{ ...state, history: {} }],
    } satisfies StatesPayload
    const { container } = render(<BrasilMap payload={noHistoryPayload} />)
    const statePath = container.querySelector('[data-uf="SP"]')
    expect(statePath).not.toBeNull()
    if (statePath === null) return

    // When
    fireEvent.mouseEnter(statePath)

    // Then
    expect(screen.queryByText('São Paulo')).not.toBeInTheDocument()
  })
})
