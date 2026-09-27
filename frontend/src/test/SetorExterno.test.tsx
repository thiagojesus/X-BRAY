import { render, screen, within } from '@testing-library/react'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'

import App from '../App'

const mockFetch = vi.fn<typeof fetch>()

const metadata = {
  conta_corrente_12m_pib: { code: 23079, unit: 'pct_gdp', frequency: 'monthly', reference_period: '2026-07-01', deadline: '2026-10-14', freshness: 'ok' },
  idp_12m_pib: { code: 23080, unit: 'pct_gdp', frequency: 'monthly', reference_period: '2026-07-01', deadline: '2026-10-14', freshness: 'ok' },
  idp_mensal_usd: { code: 22885, unit: 'usd_million', frequency: 'monthly', reference_period: '2026-07-01', deadline: '2026-10-14', freshness: 'ok' },
  balanca_comercial_usd: { code: 22707, unit: 'usd_million', frequency: 'monthly', reference_period: '2026-07-01', deadline: '2026-10-14', freshness: 'ok' },
  investimento_carteira_usd: { code: 22905, unit: 'usd_million', frequency: 'monthly', reference_period: '2026-07-01', deadline: '2026-10-14', freshness: 'ok' },
  reservas_internacionais: { code: 3546, unit: 'usd_million', frequency: 'monthly', reference_period: '2026-08-01', deadline: '2026-11-14', freshness: 'ok' },
} as const

function externalResponse(currentAccount: string | null, directInvestment: string | null) {
  return {
    source: 'BCB SGS',
    last_updated: '2026-09-21T12:00:00Z',
    status: 'ok',
    unavailable: [],
    data: {
      conta_corrente_12m_pib: [{ data: '01/07/2026', valor: currentAccount }],
      idp_12m_pib: [{ data: '01/07/2026', valor: directInvestment }],
      idp_mensal_usd: [{ data: '01/07/2026', valor: '7460.5' }],
      balanca_comercial_usd: [{ data: '01/07/2026', valor: '6152.2' }],
      investimento_carteira_usd: [{ data: '01/07/2026', valor: '-221.1' }],
      reservas_internacionais: [{ data: '01/08/2026', valor: '372616.0' }],
    },
    metadata,
  }
}

function serveExternal(currentAccount: string | null, directInvestment: string | null): void {
  mockFetch.mockImplementation(async input => {
    const url = input instanceof Request ? input.url : input.toString()
    if (url === '/api/setor-externo') return Response.json(externalResponse(currentAccount, directInvestment))
    if (url === '/api/status') return Response.json({ status: 'running' })
    return new Response(null, { status: 404 })
  })
}

beforeEach(() => {
  window.history.pushState({}, '', '/setor-externo')
  mockFetch.mockReset()
  serveExternal('-2.49', '3.5')
  vi.stubGlobal('fetch', mockFetch)
})

afterEach(() => {
  window.history.pushState({}, '', '/')
  vi.unstubAllGlobals()
  vi.restoreAllMocks()
})

describe('Setor Externo', () => {
  it('[RED XBR-302] renders the route, five KPIs, four compatible charts, and covered ratio', async () => {
    render(<App />)

    const header = screen.getByRole('banner')
    expect(await within(header).findByText('Setor Externo')).toBeInTheDocument()
    for (const label of [
      'Conta corrente 12m / PIB',
      'IDP 12m / PIB',
      'Balança comercial',
      'Investimento em carteira',
      'Reservas internacionais',
    ]) {
      expect(screen.getByText(label, { selector: '.kpi-label' })).toBeInTheDocument()
    }
    expect(screen.getByText('Conta corrente × IDP')).toBeInTheDocument()
    expect(screen.getByText('IDP mensal × investimento em carteira')).toBeInTheDocument()
    expect(screen.getByText('Balança comercial mensal')).toBeInTheDocument()
    expect(screen.getByText('Reservas internacionais', { selector: '.chart-title' })).toBeInTheDocument()
    expect(screen.getByText(/1,41×.*coberto/i)).toBeInTheDocument()
    expect(screen.getByText(/PTAX permanece em Câmbio/i)).toBeInTheDocument()
    expect(mockFetch).toHaveBeenCalledWith('/api/setor-externo', expect.objectContaining({ signal: expect.any(AbortSignal) }))
  })

  it.each([
    ['-4', '2', /0,50×.*não coberto/i],
    ['-3.5', '3.5', /1,00×.*coberto/i],
  ])('classifies deficit coverage for current account %s and IDP %s', async (currentAccount, directInvestment, expected) => {
    serveExternal(currentAccount, directInvestment)

    render(<App />)

    expect(await screen.findByText(expected)).toBeInTheDocument()
  })

  it.each([
    ['2.49', '3.5'],
    ['0', '3.5'],
    ['-2.49', null],
    [null, '3.5'],
  ])('does not claim coverage for current account %s and IDP %s', async (currentAccount, directInvestment) => {
    serveExternal(currentAccount, directInvestment)

    render(<App />)

    expect(await screen.findByText('Cobertura por IDP indisponível')).toBeInTheDocument()
    expect(screen.queryByText(/\d,\d{2}×.*coberto/i)).not.toBeInTheDocument()
    expect(screen.queryByText(/não coberto/i)).not.toBeInTheDocument()
  })

  it('preserves available values and charts for partial data without a false claim', async () => {
    const partial = externalResponse('-2.49', null)
    mockFetch.mockImplementation(async input => {
      const url = input instanceof Request ? input.url : input.toString()
      if (url === '/api/setor-externo') {
        return Response.json({ ...partial, status: 'partial', unavailable: ['idp_12m_pib'] })
      }
      if (url === '/api/status') return Response.json({ status: 'running' })
      return new Response(null, { status: 404 })
    })

    render(<App />)

    expect(await screen.findByRole('status')).toHaveTextContent('Fonte parcialmente indisponível.')
    expect(screen.getByText('-2,5% do PIB')).toBeInTheDocument()
    expect(screen.getByText('Reservas internacionais', { selector: '.chart-title' })).toBeInTheDocument()
    expect(screen.getByText('Cobertura por IDP indisponível')).toBeInTheDocument()
    expect(screen.queryByText(/NaN/)).not.toBeInTheDocument()
  })
})
