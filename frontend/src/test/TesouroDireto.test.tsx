import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest'
import { render, screen, waitFor } from '@testing-library/react'
import { MemoryRouter } from 'react-router-dom'
import TesouroDireto from '../pages/TesouroDireto'

const mockFetch = vi.fn()

const quotesData = {
  data: {
    prefixado: [
      { symbol: 'tesouro_prefixado_01-01-2027', name: 'Tesouro Prefixado 2027', indexer: 'prefixado', maturityDate: '01/01/2027', durationDays: 365, maturityLabel: 'Curto (~2 anos)', buyRate: 13.5, sellRate: 13.2, buyPrice: 600, sellPrice: 590, basePrice: 595, couponType: 'zero' },
      { symbol: 'tesouro_prefixado_01-01-2037', name: 'Tesouro Prefixado 2037', indexer: 'prefixado', maturityDate: '01/01/2037', durationDays: 4000, maturityLabel: 'Longo (~10 anos)', buyRate: 14.5, sellRate: 14.2, buyPrice: 800, sellPrice: 790, basePrice: 795, couponType: 'semestrais' },
    ],
    ipca: [
      { symbol: 'tesouro_ipcaplus_01-01-2035', name: 'Tesouro IPCA+ 2035', indexer: 'ipca', maturityDate: '01/01/2035', durationDays: 3000, maturityLabel: 'Longo (~10 anos)', buyRate: 7.5, sellRate: 7.2, buyPrice: 3000, sellPrice: 2950, basePrice: 2970, couponType: 'semestrais' },
    ],
  },
}

const catalogData = {
  data: [
    { code: 157, name: 'Tesouro Prefixado', indexer: 'prefixado', targetYear: 2027, couponType: 'semestrais', available: true },
    { code: 223, name: 'Tesouro Prefixado', indexer: 'prefixado', targetYear: 2037, couponType: 'semestrais', available: true },
    { code: 111, name: 'Tesouro IPCA+', indexer: 'ipca', targetYear: 2035, couponType: 'semestrais', available: true },
  ],
}

const historyData = {
  data: {
    code: 157,
    name: 'Tesouro Prefixado 2027',
    maturityDate: '01/01/2027',
    points: [
      { date: '20/07/2026', buyRate: 13.5, sellRate: 13.2, buyPrice: 600, sellPrice: 590 },
      { date: '21/07/2026', buyRate: 13.6, sellRate: 13.3, buyPrice: 601, sellPrice: 591 },
      { date: '14/08/2026', buyRate: 13.8, sellRate: 13.5, buyPrice: 605, sellPrice: 595 },
    ],
  },
}

function jsonResponse(payload: unknown) {
  return { ok: true, json: () => Promise.resolve(payload) }
}

function mockApiResponses() {
  mockFetch.mockImplementation((url: string) => {
    if (url.includes('/api/tesouro-direto/titulos')) {
      return Promise.resolve(jsonResponse(catalogData))
    }
    if (url.includes('/api/tesouro-direto/historico')) {
      return Promise.resolve(jsonResponse(historyData))
    }
    return Promise.resolve(jsonResponse(quotesData))
  })
}

beforeEach(() => {
  vi.stubGlobal('fetch', mockFetch)
  mockApiResponses()
})

afterEach(() => vi.restoreAllMocks())

function renderPage() {
  return render(
    <MemoryRouter>
      <TesouroDireto />
    </MemoryRouter>
  )
}

describe('TesouroDireto', () => {
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

  it('shows sem dados when data.data is null', async () => {
    mockFetch.mockImplementation((url: string) =>
      url.includes('/api/tesouro-direto/titulos')
        ? Promise.resolve(jsonResponse({ data: [] }))
        : Promise.resolve(jsonResponse({ data: null }))
    )
    renderPage()
    await waitFor(() => expect(screen.getByText(/Sem dados/)).toBeInTheDocument())
  })

  it('renders KPIs, filters and the bonds table', async () => {
    renderPage()
    await waitFor(() => expect(screen.getByText('Tesouro Prefixado 2027')).toBeInTheDocument())
    expect(screen.getAllByText('Pré-fixado').length).toBeGreaterThan(0)
    expect(screen.getAllByText('IPCA+').length).toBeGreaterThan(0)
    expect(screen.getByText('Tesouro IPCA+ 2035')).toBeInTheDocument()
    expect(screen.getByText('Títulos Disponíveis')).toBeInTheDocument()
    expect(screen.getByText('Taxa Compra')).toBeInTheDocument()
    expect(screen.queryByText('Taxa Venda')).not.toBeInTheDocument()
    expect(screen.getByText('Preço Compra')).toBeInTheDocument()
  })

  it('renders the historical chart with catalog title and series', async () => {
    renderPage()
    await waitFor(() =>
      expect(screen.getByText('Histórico de Preços e Taxas — Tesouro Prefixado 2027')).toBeInTheDocument()
    )
    expect(screen.getByText('Investimento')).toBeInTheDocument()
    expect(screen.getByText('Resgate')).toBeInTheDocument()
    expect(screen.getByRole('combobox')).toBeInTheDocument()
    expect(screen.getByText('Últimos 30 dias')).toBeInTheDocument()
    expect(screen.getByText('Taxa')).toBeInTheDocument()
    expect(screen.getByText('Preço')).toBeInTheDocument()
  })

  it('switches the historical chart period to 12 meses', async () => {
    renderPage()
    await waitFor(() =>
      expect(screen.getByText('Histórico de Preços e Taxas — Tesouro Prefixado 2027')).toBeInTheDocument()
    )
    const radio = screen.getByLabelText('Últimos 12 meses')
    radio.click()
    await waitFor(() => {
      const historyCalls = mockFetch.mock.calls.filter(call => call[0].includes('/historico'))
      expect(historyCalls.length).toBeGreaterThan(0)
      expect(historyCalls[historyCalls.length - 1][0]).toContain('days=365')
    })
  })

  it('filters the table by indexer', async () => {
    renderPage()
    await waitFor(() => expect(screen.getByText('Tesouro Prefixado 2027')).toBeInTheDocument())
    const radio = screen.getByLabelText('IPCA+')
    radio.click()
    expect(screen.queryByText('Tesouro Prefixado 2027')).not.toBeInTheDocument()
    expect(screen.getByText('Tesouro IPCA+ 2035')).toBeInTheDocument()
  })

  it('switches the table columns when the Taxa filter changes to Venda', async () => {
    renderPage()
    await waitFor(() => expect(screen.getByText('Tesouro Prefixado 2027')).toBeInTheDocument())
    expect(screen.getByText('Taxa Compra')).toBeInTheDocument()
    expect(screen.queryByText('Taxa Venda')).not.toBeInTheDocument()
    screen.getByLabelText('Venda').click()
    expect(screen.getByText('Taxa Venda')).toBeInTheDocument()
    expect(screen.queryByText('Taxa Compra')).not.toBeInTheDocument()
    expect(screen.getByText('Preço Venda')).toBeInTheDocument()
    expect(screen.queryByText('Preço Compra')).not.toBeInTheDocument()
  })

  it('filters the table by maturity', async () => {
    renderPage()
    await waitFor(() => expect(screen.getByText('Tesouro Prefixado 2027')).toBeInTheDocument())
    screen.getByLabelText('Longo (~10 anos)').click()
    expect(screen.queryByText('Tesouro Prefixado 2027')).not.toBeInTheDocument()
    expect(screen.getByText('Tesouro Prefixado 2037')).toBeInTheDocument()
    expect(screen.getByText('Tesouro IPCA+ 2035')).toBeInTheDocument()
  })

  it('reflects the maturity filter in the KPI cards', async () => {
    renderPage()
    await waitFor(() => expect(screen.getByText('Tesouro Prefixado 2027')).toBeInTheDocument())
    screen.getByLabelText('Longo (~10 anos)').click()
    const kpiValues = Array.from(document.querySelectorAll('.kpi-value')).map(el => el.textContent)
    expect(kpiValues).toContain('14.50%')
    expect(kpiValues).not.toContain('13.50%')
  })
})