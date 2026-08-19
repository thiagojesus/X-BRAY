import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest'
import { render, screen, waitFor, within } from '@testing-library/react'
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

const compararData = {
  data: [
    {
      code: 157,
      name: 'Tesouro Prefixado',
      maturityDate: '01/01/2027',
      points: [
        { date: '20/07/2026', buyRate: 13.5, sellRate: 13.2, buyPrice: 600, sellPrice: 590 },
        { date: '21/07/2026', buyRate: 13.6, sellRate: 13.3, buyPrice: 601, sellPrice: 591 },
        { date: '14/08/2026', buyRate: 13.8, sellRate: 13.5, buyPrice: 605, sellPrice: 595 },
      ],
    },
    {
      code: 223,
      name: 'Tesouro Prefixado',
      maturityDate: '01/01/2037',
      points: [
        { date: '20/07/2026', buyRate: 14.5, sellRate: 14.2, buyPrice: 800, sellPrice: 790 },
        { date: '21/07/2026', buyRate: 14.4, sellRate: 14.1, buyPrice: 801, sellPrice: 791 },
        { date: '14/08/2026', buyRate: 14.5, sellRate: 14.2, buyPrice: 802, sellPrice: 792 },
      ],
    },
    {
      code: 111,
      name: 'Tesouro IPCA+',
      maturityDate: '01/01/2035',
      points: [
        { date: '20/07/2026', buyRate: 7.5, sellRate: 7.2, buyPrice: 3000, sellPrice: 2950 },
        { date: '14/08/2026', buyRate: 7.6, sellRate: 7.3, buyPrice: 3010, sellPrice: 2960 },
      ],
    },
  ],
}

function jsonResponse(payload: unknown) {
  return { ok: true, json: () => Promise.resolve(payload) }
}

function mockApiResponses() {
  mockFetch.mockImplementation((url: string) => {
    if (url.includes('/api/tesouro-direto/titulos')) {
      return Promise.resolve(jsonResponse(catalogData))
    }
    if (url.includes('/api/tesouro-direto/comparar')) {
      return Promise.resolve(jsonResponse(compararData))
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
    await waitFor(() =>
      expect(screen.getAllByText('Tesouro Prefixado 2027').length).toBeGreaterThan(0)
    )
    expect(screen.getAllByText('Pré-fixado').length).toBeGreaterThan(0)
    expect(screen.getAllByText('IPCA+').length).toBeGreaterThan(0)
    expect(screen.getAllByText('Tesouro IPCA+ 2035').length).toBeGreaterThan(0)
    expect(screen.getByText('Títulos Disponíveis')).toBeInTheDocument()
    expect(screen.getByText('Taxa Compra')).toBeInTheDocument()
    expect(screen.queryByText('Taxa Venda')).not.toBeInTheDocument()
    expect(screen.getByText('Preço Compra')).toBeInTheDocument()
  })

  it('renders the comparison chart with default bonds, legend and inversion badge', async () => {
    renderPage()
    await waitFor(() =>
      expect(screen.getByText('Comparar Títulos — Taxas e Preços')).toBeInTheDocument()
    )
    expect(screen.getByText('Investimento')).toBeInTheDocument()
    expect(screen.getByText('Resgate')).toBeInTheDocument()
    expect(screen.getByText('Últimos 30 dias')).toBeInTheDocument()
    expect(screen.getByText('Taxa')).toBeInTheDocument()
    expect(screen.getByText('Preço')).toBeInTheDocument()
    await waitFor(() => expect(screen.getByText(/Curva NORMAL/)).toBeInTheDocument())
    expect(screen.getAllByText('Tesouro Prefixado 2027').length).toBeGreaterThan(0)
    expect(screen.getAllByText('Tesouro Prefixado 2037').length).toBeGreaterThan(0)
  })

  it('switches the comparison period to 12 meses', async () => {
    renderPage()
    await waitFor(() => expect(screen.getByText(/Curva NORMAL/)).toBeInTheDocument())
    screen.getByLabelText('Últimos 12 meses').click()
    await waitFor(() => {
      const calls = mockFetch.mock.calls.filter(call => call[0].includes('/comparar'))
      expect(calls.length).toBeGreaterThan(0)
      expect(calls[calls.length - 1][0]).toContain('days=365')
    })
  })

  it('limits bond selection to 5 and disables the 6th checkbox', async () => {
    const sixCatalog = {
      data: [
        { code: 157, name: 'Tesouro Prefixado', indexer: 'prefixado', targetYear: 2027, couponType: 'semestrais', available: true },
        { code: 158, name: 'Tesouro Prefixado', indexer: 'prefixado', targetYear: 2029, couponType: 'semestrais', available: true },
        { code: 159, name: 'Tesouro Prefixado', indexer: 'prefixado', targetYear: 2031, couponType: 'semestrais', available: true },
        { code: 223, name: 'Tesouro Prefixado', indexer: 'prefixado', targetYear: 2037, couponType: 'semestrais', available: true },
        { code: 111, name: 'Tesouro IPCA+', indexer: 'ipca', targetYear: 2035, couponType: 'semestrais', available: true },
        { code: 112, name: 'Tesouro IPCA+', indexer: 'ipca', targetYear: 2045, couponType: 'semestrais', available: true },
      ],
    }
    mockFetch.mockImplementation((url: string) => {
      if (url.includes('/api/tesouro-direto/titulos')) {
        return Promise.resolve(jsonResponse(sixCatalog))
      }
      if (url.includes('/api/tesouro-direto/comparar')) {
        return Promise.resolve(jsonResponse(compararData))
      }
      return Promise.resolve(jsonResponse(quotesData))
    })
    renderPage()
    await waitFor(() => expect(screen.getByLabelText('Tesouro Prefixado 2029')).toBeInTheDocument())
    screen.getByLabelText('Tesouro Prefixado 2029').click()
    screen.getByLabelText('Tesouro IPCA+ 2035').click()
    const sixth = screen.getByLabelText('Tesouro IPCA+ 2045') as HTMLInputElement
    expect(sixth.disabled).toBe(true)
  })

  it('excludes unavailable bonds (available=false) from comparison chips and default selection', async () => {
    const catalogWithUnavailable = {
      data: [
        { code: 157, name: 'Tesouro Prefixado', indexer: 'prefixado', targetYear: 2027, couponType: 'semestrais', available: false },
        { code: 158, name: 'Tesouro Prefixado', indexer: 'prefixado', targetYear: 2029, couponType: 'semestrais', available: true },
        { code: 223, name: 'Tesouro Prefixado', indexer: 'prefixado', targetYear: 2037, couponType: 'semestrais', available: true },
        { code: 111, name: 'Tesouro IPCA+', indexer: 'ipca', targetYear: 2035, couponType: 'semestrais', available: true },
      ],
    }
    mockFetch.mockImplementation((url: string) => {
      if (url.includes('/api/tesouro-direto/titulos')) {
        return Promise.resolve(jsonResponse(catalogWithUnavailable))
      }
      if (url.includes('/api/tesouro-direto/comparar')) {
        return Promise.resolve(jsonResponse(compararData))
      }
      return Promise.resolve(jsonResponse(quotesData))
    })
    renderPage()
    await waitFor(() =>
      expect(screen.getByText('Comparar Títulos — Taxas e Preços')).toBeInTheDocument()
    )
    expect(screen.queryByLabelText('Tesouro Prefixado 2027')).not.toBeInTheDocument()
    expect(screen.getByLabelText('Tesouro Prefixado 2029')).toBeInTheDocument()
    expect(screen.getByLabelText('Tesouro Prefixado 2037')).toBeInTheDocument()
    await waitFor(() => {
      const calls = mockFetch.mock.calls.filter(call => call[0].includes('/comparar'))
      expect(calls.length).toBeGreaterThan(0)
      expect(calls[calls.length - 1][0]).not.toContain('codes=157')
      expect(calls[calls.length - 1][0]).toContain('codes=158')
    })
  })

  it('shows the Curva INVERTIDA badge when the short bond rate exceeds the long bond', async () => {
    const invertedComparar = {
      data: [
        {
          code: 157,
          name: 'Tesouro Prefixado',
          maturityDate: '01/01/2027',
          points: [
            { date: '20/07/2026', buyRate: 13.5, sellRate: 13.2, buyPrice: 600, sellPrice: 590 },
            { date: '14/08/2026', buyRate: 15.2, sellRate: 14.9, buyPrice: 598, sellPrice: 588 },
          ],
        },
        {
          code: 223,
          name: 'Tesouro Prefixado',
          maturityDate: '01/01/2037',
          points: [
            { date: '20/07/2026', buyRate: 14.5, sellRate: 14.2, buyPrice: 800, sellPrice: 790 },
            { date: '14/08/2026', buyRate: 14.1, sellRate: 13.9, buyPrice: 805, sellPrice: 795 },
          ],
        },
      ],
    }
    mockFetch.mockImplementation((url: string) => {
      if (url.includes('/api/tesouro-direto/titulos')) {
        return Promise.resolve(jsonResponse(catalogData))
      }
      if (url.includes('/api/tesouro-direto/comparar')) {
        return Promise.resolve(jsonResponse(invertedComparar))
      }
      return Promise.resolve(jsonResponse(quotesData))
    })
    renderPage()
    await waitFor(() => expect(screen.getByText(/Curva INVERTIDA/)).toBeInTheDocument())
    expect(screen.getByText(/15.20%/)).toBeInTheDocument()
  })

  it('hides the inversion badge when the metric is Preço', async () => {
    renderPage()
    await waitFor(() => expect(screen.getByText(/Curva NORMAL/)).toBeInTheDocument())
    screen.getByLabelText('Preço').click()
    expect(screen.queryByText(/Curva NORMAL/)).not.toBeInTheDocument()
    expect(screen.queryByText(/Curva INVERTIDA/)).not.toBeInTheDocument()
  })

  it('filters the table by indexer', async () => {
    renderPage()
    await waitFor(() => expect(screen.getByText('Títulos Disponíveis')).toBeInTheDocument())
    const table = screen.getByRole('table')
    expect(within(table).getByText('Tesouro Prefixado 2027')).toBeInTheDocument()
    screen.getByLabelText('IPCA+').click()
    expect(within(table).queryByText('Tesouro Prefixado 2027')).not.toBeInTheDocument()
    expect(within(table).getByText('Tesouro IPCA+ 2035')).toBeInTheDocument()
  })

  it('switches the table columns when the Taxa filter changes to Venda', async () => {
    renderPage()
    await waitFor(() => expect(screen.getByText('Títulos Disponíveis')).toBeInTheDocument())
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
    await waitFor(() => expect(screen.getByText('Títulos Disponíveis')).toBeInTheDocument())
    const table = screen.getByRole('table')
    expect(within(table).getByText('Tesouro Prefixado 2027')).toBeInTheDocument()
    screen.getByLabelText('Longo (~10 anos)').click()
    expect(within(table).queryByText('Tesouro Prefixado 2027')).not.toBeInTheDocument()
    expect(within(table).getByText('Tesouro Prefixado 2037')).toBeInTheDocument()
    expect(within(table).getByText('Tesouro IPCA+ 2035')).toBeInTheDocument()
  })

  it('reflects the maturity filter in the KPI cards', async () => {
    renderPage()
    await waitFor(() => expect(screen.getByText('Títulos Disponíveis')).toBeInTheDocument())
    screen.getByLabelText('Longo (~10 anos)').click()
    const kpiValues = Array.from(document.querySelectorAll('.kpi-value')).map(el => el.textContent)
    expect(kpiValues).toContain('14.50%')
    expect(kpiValues).not.toContain('13.50%')
  })
})