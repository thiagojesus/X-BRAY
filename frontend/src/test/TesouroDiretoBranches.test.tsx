import { fireEvent, render, screen, waitFor, within } from '@testing-library/react'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { MemoryRouter } from 'react-router-dom'

import TesouroDireto from '../pages/TesouroDireto'

type ApiFixture = {
  readonly quotes: unknown
  readonly catalog: unknown
  readonly comparison: unknown
  readonly comparisonError?: Error
}

const mockFetch = vi.fn<typeof fetch>()

const quoteWithNullValues = {
  symbol: 'tesouro_prefixado_01-01-2030',
  name: 'Tesouro Prefixado 2030',
  indexer: 'prefixado',
  maturityDate: '01/01/2030',
  durationDays: 1200,
  maturityLabel: 'Médio (~5 anos)',
  buyRate: null,
  sellRate: null,
  buyPrice: null,
  sellPrice: null,
  basePrice: null,
  couponType: 'zero',
}

const standardQuote = {
  ...quoteWithNullValues,
  buyRate: 10,
  sellRate: 9.8,
  buyPrice: 900,
  sellPrice: 890,
}

const prefixadoCatalog = [
  { code: 1, name: 'Tesouro Prefixado', indexer: 'prefixado', targetYear: 2030, couponType: 'zero', available: true },
  { code: 2, name: 'Tesouro Prefixado', indexer: 'prefixado', targetYear: 2040, couponType: 'zero', available: true },
]

function installApi(fixture: ApiFixture): void {
  mockFetch.mockImplementation((input) => {
    const url = String(input)
    if (url.includes('/api/tesouro-direto/titulos')) {
      return Promise.resolve(Response.json(fixture.catalog))
    }
    if (url.includes('/api/tesouro-direto/comparar')) {
      if (fixture.comparisonError) return Promise.reject(fixture.comparisonError)
      return Promise.resolve(Response.json(fixture.comparison))
    }
    return Promise.resolve(Response.json(fixture.quotes))
  })
}

function renderPage() {
  return render(
    <MemoryRouter>
      <TesouroDireto />
    </MemoryRouter>,
  )
}

beforeEach(() => {
  mockFetch.mockReset()
  vi.stubGlobal('fetch', mockFetch)
})

afterEach(() => {
  vi.unstubAllGlobals()
  vi.restoreAllMocks()
})

describe('TesouroDireto edge behavior', () => {
  it('renders missing buy and sell values as unavailable', async () => {
    // Given
    installApi({
      quotes: { data: { prefixado: [quoteWithNullValues], ipca: [] } },
      catalog: { data: prefixadoCatalog },
      comparison: { data: [] },
    })

    // When
    renderPage()
    const table = await screen.findByRole('table')

    // Then
    expect(within(table).getAllByText('-')).toHaveLength(2)
    expect(document.querySelector('.kpi-value')).toHaveTextContent('-')

    // When
    fireEvent.click(screen.getByLabelText('Venda'))

    // Then
    expect(screen.getByText('Taxa Venda')).toBeInTheDocument()
    expect(within(table).getAllByText('-')).toHaveLength(2)
  })

  it('labels a bond without target year and allows deselection', async () => {
    // Given
    const catalog = [
      { code: 1, name: 'Tesouro Prefixado', indexer: 'prefixado', targetYear: null, couponType: 'zero', available: true },
      { code: 2, name: 'Tesouro IPCA+', indexer: 'ipca', targetYear: 2035, couponType: 'zero', available: true },
    ]
    installApi({
      quotes: { data: { prefixado: [standardQuote], ipca: [] } },
      catalog: { data: catalog },
      comparison: { data: [] },
    })
    renderPage()
    const noYearBond = await screen.findByLabelText('Tesouro Prefixado')

    // When
    fireEvent.click(noYearBond)

    // Then
    await waitFor(() => expect(screen.getByText('Títulos (1/5):')).toBeInTheDocument())
  })

  it('builds sell-price comparison data and hides the rate badge', async () => {
    // Given
    const comparison = {
      data: [
        {
          code: 1,
          name: 'Tesouro Prefixado',
          maturityDate: '01/01/2030',
          points: [{ date: '01/09/2026', buyRate: 10, sellRate: 9.8, buyPrice: 900, sellPrice: 890 }],
        },
        {
          code: 2,
          name: 'Tesouro Prefixado',
          maturityDate: '01/01/2040',
          points: [{ date: '01/09/2026', buyRate: 12, sellRate: 11.8, buyPrice: 700, sellPrice: 690 }],
        },
      ],
    }
    installApi({
      quotes: { data: { prefixado: [standardQuote], ipca: [] } },
      catalog: { data: prefixadoCatalog },
      comparison,
    })
    renderPage()
    await screen.findByText(/Curva NORMAL/)

    // When
    fireEvent.click(screen.getByLabelText('Resgate'))
    fireEvent.click(screen.getByLabelText('Preço'))

    // Then
    expect(screen.queryByText(/Curva NORMAL/)).not.toBeInTheDocument()
    expect(screen.queryByText(/Curva INVERTIDA/)).not.toBeInTheDocument()
  })

  it('reports an empty comparison catalog independently of the quote table', async () => {
    // Given
    installApi({
      quotes: { data: { prefixado: [standardQuote], ipca: [] } },
      catalog: { data: [] },
      comparison: { data: [] },
    })

    // When
    renderPage()

    // Then
    expect(await screen.findByText(/Sem dados disponíveis/)).toBeInTheDocument()
    expect(await screen.findByRole('table')).toBeInTheDocument()
  })

  it('shows a comparison request failure without hiding the quote table', async () => {
    // Given
    installApi({
      quotes: { data: { prefixado: [standardQuote], ipca: [] } },
      catalog: { data: prefixadoCatalog },
      comparison: { data: [] },
      comparisonError: new Error('comparison unavailable'),
    })

    // When
    renderPage()

    // Then
    expect(await screen.findByText(/comparison unavailable/)).toBeInTheDocument()
    expect(await screen.findByRole('table')).toBeInTheDocument()
  })
})
