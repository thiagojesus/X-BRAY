import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest'
import { render, screen, waitFor } from '@testing-library/react'
import { MemoryRouter } from 'react-router-dom'
import Juros from '../pages/Juros'

const mockFetch = vi.fn()

function jsonResponse(data: unknown) {
  return { ok: true, json: () => Promise.resolve(data) }
}

function apiJuros() {
  return {
    data: {
      selic_meta: [
        { data: '07/05/2025', valor: '14,50' },
        { data: '18/06/2025', valor: '14,65' },
        { data: '06/08/2025', valor: '14,75' },
      ],
      selic_efetiva: [
        { data: '06/06/2025', valor: '14,80' },
        { data: '07/07/2025', valor: '14,90' },
      ],
      cdi: [
        { data: '06/06/2025', valor: '14,80' },
        { data: '07/07/2025', valor: '14,90' },
      ],
      tr: [
        { data: '31/12/2024', dataFim: '30/01/2025', valor: '0,70' },
        { data: '06/06/2025', dataFim: '06/07/2025', valor: '0,75' },
        { data: '07/07/2025', dataFim: '06/08/2025', valor: '0,80' },
        { data: '07/07/2025', dataFim: '07/08/2025', valor: '0,81' },
      ],
      unknown_rate: [
        { data: '07/07/2025', valor: '10,00' },
      ],
    }
  }
}

function apiCurvasDi() {
  return {
    source: 'B3 Price Report (SPR)',
    days: 30,
    dates: ['12/08/2026', '13/08/2026', '14/08/2026'],
    curves: {
      '12/08/2026': [
        { symbol: 'DI1U26', maturity: '2026-09-01', rate: 13.902 },
        { symbol: 'DI1V26', maturity: '2026-10-01', rate: 13.843 },
      ],
      '13/08/2026': [
        { symbol: 'DI1U26', maturity: '2026-09-01', rate: 13.905 },
        { symbol: 'DI1V26', maturity: '2026-10-01', rate: 13.85 },
      ],
      '14/08/2026': [
        { symbol: 'DI1U26', maturity: '2026-09-01', rate: 13.904 },
        { symbol: 'DI1V26', maturity: '2026-10-01', rate: 13.852 },
      ],
    }
  }
}

beforeEach(() => {
  vi.stubGlobal('fetch', mockFetch)
})

afterEach(() => vi.restoreAllMocks())

function renderPage() {
  return render(
    <MemoryRouter>
      <Juros />
    </MemoryRouter>
  )
}

describe('Juros', () => {
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

  it('shows no-data state', async () => {
    mockFetch.mockResolvedValue(jsonResponse({ data: null }))
    renderPage()
    await waitFor(() => expect(screen.getByText('Erro ao carregar dados: Sem dados')).toBeInTheDocument())
  })

  it('renders KPI cards and charts with multi-date data', async () => {
    mockFetch
      .mockImplementationOnce(() => Promise.resolve(jsonResponse(apiJuros())))
      .mockImplementationOnce(() => Promise.resolve(jsonResponse(apiCurvasDi())))
    renderPage()
    await waitFor(() => {
      expect(screen.getByText('Selic Meta')).toBeInTheDocument()
      expect(screen.getByText('Selic Efetiva')).toBeInTheDocument()
      expect(screen.getByText('CDI')).toBeInTheDocument()
      expect(screen.getByText('TR')).toBeInTheDocument()
      expect(screen.getByText('14.8% a.a.')).toBeInTheDocument()
    })
    expect(screen.getByText('Taxa Meta Selic (reuniões COPOM)')).toBeInTheDocument()
    expect(screen.getByText('Taxas de Mercado (anualizadas)')).toBeInTheDocument()
    expect(screen.getByText('Curvas de Juros Futuros (DI)')).toBeInTheDocument()
    expect(screen.getByText('07/07/2025 a 06/08/2025: 0.8000% no período')).toBeInTheDocument()
    expect(screen.getByText('07/07/2025 a 07/08/2025: 0.8100% no período')).toBeInTheDocument()
  })

  it('renders with empty data arrays', async () => {
    mockFetch
      .mockImplementationOnce(() => Promise.resolve(jsonResponse({ data: { selic_meta: [], selic_efetiva: [], cdi: [], tr: [] } })))
      .mockImplementationOnce(() => Promise.resolve(jsonResponse(apiCurvasDi())))
    renderPage()
    await waitFor(() => {
      expect(screen.getByText('Selic Meta')).toBeInTheDocument()
    })
  })

  it('uses a TR KPI only when the latest start date has one period', async () => {
    const singlePeriod = apiJuros()
    singlePeriod.data.tr = [
      { data: '31/12/2024', dataFim: '30/01/2025', valor: '0.7' },
      { data: '01/01/2025', dataFim: '31/01/2025', valor: '0.8' },
    ]
    mockFetch
      .mockImplementationOnce(() => Promise.resolve(jsonResponse(singlePeriod)))
      .mockImplementationOnce(() => Promise.resolve(jsonResponse(apiCurvasDi())))
    renderPage()
    await waitFor(() => expect(screen.getByText('TR')).toBeInTheDocument())
    expect(screen.getByText('01/01/2025–31/01/2025')).toBeInTheDocument()
    expect(screen.getByText('0.8000% no período')).toBeInTheDocument()
    expect(screen.getByText('01/01/2025 a 31/01/2025: 0.8000% no período')).toBeInTheDocument()
  })

  it('renders incomplete TR metadata without fabricating a period end or rate', async () => {
    const incomplete = apiJuros()
    incomplete.data.tr = [{ data: '02/01/2025', dataFim: '', valor: '' }]
    mockFetch
      .mockImplementationOnce(() => Promise.resolve(jsonResponse(incomplete)))
      .mockImplementationOnce(() => Promise.resolve(jsonResponse(apiCurvasDi())))
    renderPage()
    await waitFor(() => expect(screen.getByText('TR')).toBeInTheDocument())
    expect(screen.getByText('02/01/2025 a fim não informado: —% no período')).toBeInTheDocument()
    expect(screen.queryByText('02/01/2025–')).not.toBeInTheDocument()
  })

  it('shows DI curve empty state when no curve dates exist', async () => {
    mockFetch
      .mockImplementationOnce(() => Promise.resolve(jsonResponse(apiJuros())))
      .mockImplementationOnce(() => Promise.resolve(jsonResponse({ source: 'B3 Price Report (SPR)', days: 30, dates: [], curves: {} })))
    renderPage()
    await waitFor(() => {
      expect(screen.getByText('Curvas de Juros Futuros (DI)')).toBeInTheDocument()
    })
    expect(screen.getByText('Sem dados disponíveis')).toBeInTheDocument()
  })
})
