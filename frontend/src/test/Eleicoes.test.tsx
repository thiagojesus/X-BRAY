import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest'
import { render, screen, waitFor } from '@testing-library/react'
import { MemoryRouter } from 'react-router-dom'
import Eleicoes from '../pages/Eleicoes'

const mockFetch = vi.fn()

const ELECTIONS_RESPONSE = {
  source: 'Polymarket',
  updated_at: '2026-08-19T00:00:00+00:00',
  national: {
    candidates: [
      { name: 'Luiz Inácio Lula da Silva', price: 0.64, volume: 50000000 },
      { name: 'Flávio Bolsonaro', price: 0.33, volume: 40000000 },
    ],
    history: {
      '2025-09-19': { 'Luiz Inácio Lula da Silva': 59, 'Flávio Bolsonaro': 2 },
      '2025-09-20': { 'Luiz Inácio Lula da Silva': 49, 'Flávio Bolsonaro': 3 },
      '2025-09-21': { 'Luiz Inácio Lula da Silva': 53, 'Flávio Bolsonaro': 5 },
    },
  },
  days: ['2025-09-21'],
  ufs: [
    {
      uf: 'SP',
      candidates: [
        { name: 'Luiz Inácio Lula da Silva', price: 0.55, volume: 1000 },
        { name: 'Flávio Bolsonaro', price: 0.45, volume: 900 },
      ],
      history: {
        '2025-09-21': { 'Luiz Inácio Lula da Silva': 55, 'Flávio Bolsonaro': 45 },
      },
    },
  ],
}

beforeEach(() => {
  mockFetch.mockReset()
  vi.stubGlobal('fetch', mockFetch)
})

afterEach(() => vi.restoreAllMocks())

function renderPage() {
  return render(
    <MemoryRouter>
      <Eleicoes />
    </MemoryRouter>
  )
}

describe('Eleicoes', () => {
  it('shows loading state', () => {
    mockFetch.mockReturnValue(new Promise(() => {}))
    renderPage()
    expect(screen.getByText('Carregando dados...')).toBeInTheDocument()
  })

  it('shows error state', async () => {
    mockFetch.mockRejectedValue(new Error('fail'))
    renderPage()
    await waitFor(() => expect(screen.getByText(/Erro ao carregar dados/)).toBeInTheDocument())
  })

  it('shows no-data message when the backend snapshot is empty', async () => {
    mockFetch.mockResolvedValue({
      ok: true,
      json: () => Promise.resolve({ national: { candidates: [], history: {} }, days: [], ufs: [] }),
    })
    renderPage()
    await waitFor(() => {
      expect(screen.getByText('Sem dados disponíveis')).toBeInTheDocument()
    })
  })

  it('renders the cached national and state snapshot without provider requests', async () => {
    mockFetch.mockResolvedValue({
      ok: true,
      json: () => Promise.resolve(ELECTIONS_RESPONSE),
    })

    renderPage()
    await waitFor(() => {
      expect(screen.getAllByText('Luiz Inácio Lula da Silva')).toHaveLength(2)
      expect(screen.getAllByText('Flávio Bolsonaro')).toHaveLength(1)
    })
    expect(screen.getByText('64.0%')).toBeInTheDocument()
    expect(screen.getByText('33.0%')).toBeInTheDocument()
    expect(screen.queryByText(/Person N/)).not.toBeInTheDocument()
    expect(screen.getByText('Probabilidade de vitória — Presidente do Brasil 2026')).toBeInTheDocument()
    expect(screen.getByText('Mapa — 1º lugar no 1º turno por estado')).toBeInTheDocument()
    expect(mockFetch).toHaveBeenCalledTimes(1)
    expect(mockFetch).toHaveBeenCalledWith('/api/eleicoes/estados')
  })
})
