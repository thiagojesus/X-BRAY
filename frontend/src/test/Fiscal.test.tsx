import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { render, screen, within } from '@testing-library/react'
import App from '../App'
import { formatIndicatorValue } from '../utils/format'
import { mergeSeriesByDate } from '../utils/series'

const mockFetch = vi.fn<typeof fetch>()

const fiscalResponse = {
  source: 'BCB SGS',
  last_updated: '2026-09-01T12:00:00Z',
  status: 'ok',
  unavailable: [],
  data: {
    dbgg_pib: [{ data: '01/07/2026', valor: '82.56' }],
    dlsp_pib: [{ data: '01/07/2026', valor: '69.13' }],
    saldo_primario_12m_pib: [{ data: '01/07/2026', valor: '1.20' }],
    juros_nominais_12m_pib: [{ data: '01/07/2026', valor: '6.00' }],
    saldo_nominal_12m_pib: [{ data: '01/07/2026', valor: '-2.50' }],
  },
  metadata: {
    dbgg_pib: { code: 13762, unit: 'pct_gdp', frequency: 'monthly', freshness: 'ok', sign_convention: 'debt_positive' },
    dlsp_pib: { code: 4513, unit: 'pct_gdp', frequency: 'monthly', freshness: 'ok', sign_convention: 'debt_positive' },
    saldo_primario_12m_pib: { code: 5793, unit: 'pct_gdp', frequency: 'monthly', freshness: 'ok', sign_convention: 'surplus_positive' },
    juros_nominais_12m_pib: { code: 5760, unit: 'pct_gdp', frequency: 'monthly', freshness: 'ok', sign_convention: 'cost_positive' },
    saldo_nominal_12m_pib: { code: 5727, unit: 'pct_gdp', frequency: 'monthly', freshness: 'ok', sign_convention: 'surplus_positive' },
  },
}

beforeEach(() => {
  window.history.pushState({}, '', '/fiscal')
  mockFetch.mockReset()
  mockFetch.mockImplementation(async (input) => {
    const url = input instanceof Request ? input.url : input.toString()
    if (url === '/api/fiscal') return Response.json(fiscalResponse)
    if (url === '/api/status') return Response.json({ status: 'running' })
    if (url === '/api/data-quality/fiscal') return Response.json({ status: 'ok', metadata: {} })
    return new Response(null, { status: 404 })
  })
  vi.stubGlobal('fetch', mockFetch)
})

afterEach(() => {
  window.history.pushState({}, '', '/')
  vi.unstubAllGlobals()
  vi.restoreAllMocks()
})

describe('Fiscal', () => {
  it('characterizes existing fiscal formatting and date merge utilities', () => {
    const rows = mergeSeriesByDate({
      saldo_primario_12m_pib: [{ data: '01/07/2026', valor: '1.20' }],
      saldo_nominal_12m_pib: [{ data: '01/07/2026', valor: '-2.50' }],
    })

    expect(rows).toEqual([{
      date: '01/07/2026',
      saldo_primario_12m_pib: 1.2,
      saldo_nominal_12m_pib: -2.5,
    }])
    expect(formatIndicatorValue('82.56', 'pct_gdp')).toBe('82,6% do PIB')
  })

  it('[RED XBR-102] renders five fiscal KPIs, three charts, and explicit sign semantics', async () => {
    render(<App />)

    const header = screen.getByRole('banner')
    expect(await within(header).findByText('Fiscal')).toBeInTheDocument()
    expect(screen.getByText('DBGG (% do PIB)', { selector: '.kpi-label' })).toBeInTheDocument()
    expect(screen.getByText('DLSP (% do PIB)', { selector: '.kpi-label' })).toBeInTheDocument()
    expect(screen.getByText('Saldo primário 12m', { selector: '.kpi-label' })).toBeInTheDocument()
    expect(screen.getByText('Juros nominais 12m', { selector: '.kpi-label' })).toBeInTheDocument()
    expect(screen.getByText('Saldo nominal 12m', { selector: '.kpi-label' })).toBeInTheDocument()
    expect(screen.getByText('DBGG × DLSP')).toBeInTheDocument()
    expect(screen.getByText('Saldos primário × nominal')).toBeInTheDocument()
    expect(screen.getByText('Juros nominais')).toBeInTheDocument()
    expect(screen.getByText('Superávit · +1,2% do PIB')).toBeInTheDocument()
    expect(screen.getByText('Déficit · −2,5% do PIB')).toBeInTheDocument()
    expect(screen.getByText('Custo · 6,0% do PIB')).toBeInTheDocument()
    expect(screen.getByText((_content, element) => (
      element?.tagName === 'P' && element.textContent?.includes('DBGG é a Dívida Bruta do Governo Geral') === true
    ))).toBeInTheDocument()
    expect(screen.getByText(/NFSP/)).toBeInTheDocument()
    expect(mockFetch).toHaveBeenCalledWith('/api/fiscal', expect.objectContaining({ signal: expect.any(AbortSignal) }))
  })
})
