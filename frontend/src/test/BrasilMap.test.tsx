import { describe, it, expect } from 'vitest'
import { render, screen } from '@testing-library/react'
import BrasilMap, { buildCandidateColors, computeUFsForDay, formatDay, type UFData } from '../charts/BrasilMap'

const sampleUFs: UFData[] = [
  {
    uf: 'SP',
    candidates: [
      { name: 'Flavio Bolsonaro', price: 0.89, volume: 3000 },
      { name: 'Lula', price: 0.08, volume: 1027 },
    ],
    history: {
      '2026-08-18': { 'Flavio Bolsonaro': 84.5, Lula: 14.0 },
      '2026-08-19': { 'Flavio Bolsonaro': 88.5, Lula: 5.6 },
    },
  },
  {
    uf: 'BA',
    candidates: [
      { name: 'Lula', price: 0.97, volume: 5000 },
      { name: 'Flavio Bolsonaro', price: 0.02, volume: 300 },
    ],
    history: {
      '2026-08-18': { Lula: 95.0, 'Flavio Bolsonaro': 3.0 },
      '2026-08-19': { Lula: 97.8, 'Flavio Bolsonaro': 1.2 },
    },
  },
  {
    uf: 'AC',
    candidates: [{ name: 'Flavio Bolsonaro', price: 0.95, volume: 800 }],
    history: {
      '2026-08-19': { 'Flavio Bolsonaro': 96.75 },
    },
  },
]

describe('buildCandidateColors', () => {
  it('assigns consistent colors by total volume', () => {
    const colors = buildCandidateColors(sampleUFs)
    expect(colors.get('Flavio Bolsonaro')).toBeTruthy()
    expect(colors.get('Lula')).toBeTruthy()
    expect(colors.get('Flavio Bolsonaro')).toBe(colors.get('Flavio Bolsonaro'))
  })

  it('assigns different colors to different candidates', () => {
    const colors = buildCandidateColors(sampleUFs)
    expect(colors.get('Lula')).not.toBe(colors.get('Flavio Bolsonaro'))
  })
})

describe('computeUFsForDay', () => {
  it('computes leader, margin and opacity', () => {
    const colors = buildCandidateColors(sampleUFs)
    const result = computeUFsForDay(sampleUFs, '2026-08-19', colors)
    const sp = result.find(r => r.uf === 'SP')!
    expect(sp.leader).toBe('Flavio Bolsonaro')
    expect(sp.leaderPct).toBe(88.5)
    expect(sp.runnerUpPct).toBe(5.6)
    expect(sp.margin).toBeCloseTo(82.9)
    expect(sp.opacity).toBeCloseTo(0.8125)
    expect(sp.color).toBe(colors.get('Flavio Bolsonaro'))
  })

  it('returns empty state for missing day', () => {
    const colors = buildCandidateColors(sampleUFs)
    const result = computeUFsForDay(sampleUFs, '2020-01-01', colors)
    expect(result[0].leader).toBeNull()
    expect(result[0].opacity).toBe(0.12)
  })

  it('handles single-candidate states', () => {
    const colors = buildCandidateColors(sampleUFs)
    const result = computeUFsForDay(sampleUFs, '2026-08-19', colors)
    const ac = result.find(r => r.uf === 'AC')!
    expect(ac.leader).toBe('Flavio Bolsonaro')
    expect(ac.margin).toBe(96.75)
  })
})

describe('formatDay', () => {
  it('converts ISO to DD/MM/YYYY', () => {
    expect(formatDay('2026-08-19')).toBe('19/08/2026')
  })
})

describe('BrasilMap', () => {
  const payload = {
    source: 'Polymarket',
    updated_at: '2026-08-19T20:00:00Z',
    days: ['2026-08-18', '2026-08-19'],
    ufs: sampleUFs,
  }

  it('renders map title and slider', () => {
    render(<BrasilMap payload={payload} />)
    expect(screen.getByText('Mapa — 1º lugar no 1º turno por estado')).toBeInTheDocument()
    expect(document.querySelector('.date-slider')).toBeInTheDocument()
    expect(document.querySelector('.mapa-svg')).toBeInTheDocument()
  })

  it('defaults to last available day', () => {
    render(<BrasilMap payload={payload} />)
    expect(screen.getByText('19/08/2026')).toBeInTheDocument()
  })

  it('renders legend with candidate names', () => {
    render(<BrasilMap payload={payload} />)
    expect(screen.getByText('Flavio Bolsonaro')).toBeInTheDocument()
    expect(screen.getByText('Lula')).toBeInTheDocument()
  })

  it('renders 27 state paths', () => {
    const { container } = render(<BrasilMap payload={payload} />)
    expect(container.querySelectorAll('.uf-path').length).toBe(27)
  })

  it('shows empty message when no data for day', () => {
    const emptyPayload = { ...payload, days: ['2020-01-01'] }
    render(<BrasilMap payload={emptyPayload} />)
    expect(screen.getByText('Sem dados para este dia')).toBeInTheDocument()
  })
})