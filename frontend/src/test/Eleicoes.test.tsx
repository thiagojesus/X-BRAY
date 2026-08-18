import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest'
import { render, screen, waitFor } from '@testing-library/react'
import { MemoryRouter } from 'react-router-dom'
import Eleicoes from '../pages/Eleicoes'

const mockFetch = vi.fn()

const EVENT_RESPONSE = [{
  id: '45915',
  title: 'Brazil Presidential Election',
  markets: [
    {
      question: 'Will Luiz Inácio Lula da Silva win the 2026 Brazilian presidential election?',
      lastTradePrice: 0.64,
      volume: 50000000,
      clobTokenIds: '["111","222"]',
    },
    {
      question: 'Will Flávio Bolsonaro win the 2026 Brazilian presidential election?',
      lastTradePrice: 0.33,
      volume: 40000000,
      clobTokenIds: '["333","444"]',
    },
    {
      question: 'Will Person N win the 2026 Brazilian presidential election?',
      lastTradePrice: 0.0,
      volume: 0,
      clobTokenIds: '["555","666"]',
    },
  ],
}]

const LULA_HISTORY = {
  history: [
    { t: 1758240000, p: 0.59 },
    { t: 1758326400, p: 0.49 },
    { t: 1758412800, p: 0.53 },
  ],
}

const FLAVIO_HISTORY = {
  history: [
    { t: 1758240000, p: 0.02 },
    { t: 1758326400, p: 0.03 },
    { t: 1758412800, p: 0.05 },
  ],
}

beforeEach(() => {
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

  it('shows no-data message when event has no markets', async () => {
    mockFetch.mockResolvedValue({ ok: true, json: () => Promise.resolve([]) })
    renderPage()
    await waitFor(() => {
      expect(screen.getByText('Sem dados disponíveis')).toBeInTheDocument()
    })
  })

  it('renders KPIs and chart with candidate history, excluding placeholders', async () => {
    mockFetch.mockImplementation((url: string) => {
      if (url.includes('gamma-api.polymarket.com/events')) {
        return Promise.resolve({ ok: true, json: () => Promise.resolve(EVENT_RESPONSE) })
      }
      if (url.includes('prices-history?market=111')) {
        return Promise.resolve({ ok: true, json: () => Promise.resolve(LULA_HISTORY) })
      }
      if (url.includes('prices-history?market=333')) {
        return Promise.resolve({ ok: true, json: () => Promise.resolve(FLAVIO_HISTORY) })
      }
      return Promise.reject(new Error('unexpected url'))
    })

    renderPage()
    await waitFor(() => {
      expect(screen.getByText('Luiz Inácio Lula da Silva')).toBeInTheDocument()
      expect(screen.getByText('Flávio Bolsonaro')).toBeInTheDocument()
    })
    expect(screen.getByText('64.0%')).toBeInTheDocument()
    expect(screen.getByText('33.0%')).toBeInTheDocument()
    expect(screen.queryByText(/Person N/)).not.toBeInTheDocument()
    expect(screen.getByText('Probabilidade de vitória — Presidente do Brasil 2026')).toBeInTheDocument()
  })
})
