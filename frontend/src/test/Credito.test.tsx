import { render, screen, within } from '@testing-library/react'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'

import App from '../App'

const mockFetch = vi.fn<typeof fetch>()

const creditoResponse = {
  status: 'ok',
  unavailable: [],
  data: {
    inadimplencia_pf: [
      { data: '01/02/2026', valor: '3.2' },
      { data: '01/03/2026', valor: '3.4' },
    ],
    endividamento_familias: [
      { data: '01/02/2026', valor: '48.5' },
      { data: '01/03/2026', valor: '48.7' },
    ],
    comprometimento_renda: [
      { data: '01/02/2026', valor: '27.5' },
      { data: '01/03/2026', valor: '27.1' },
    ],
    icc_pf: [
      { data: '01/02/2026', valor: '22.7' },
      { data: '01/03/2026', valor: '22.8' },
    ],
    taxa_media_credito_pf: [
      { data: '01/02/2026', valor: '14.4' },
      { data: '01/04/2026', valor: '14.2' },
    ],
  },
  metadata: {
    inadimplencia_pf: { code: 21084, unit: 'pct', frequency: 'monthly', reference_period: '2026-03-01', deadline: '2026-05-15', freshness: 'ok' },
    endividamento_familias: { code: 29037, unit: 'pct', frequency: 'monthly', reference_period: '2026-03-01', deadline: '2026-06-14', freshness: 'ok' },
    comprometimento_renda: { code: 29034, unit: 'pct', frequency: 'monthly', reference_period: '2026-03-01', deadline: '2026-06-14', freshness: 'ok' },
    icc_pf: { code: 25353, unit: 'pct_annual', frequency: 'monthly', reference_period: '2026-03-01', deadline: '2026-05-15', freshness: 'ok' },
    taxa_media_credito_pf: { code: 20716, unit: 'pct_annual', frequency: 'monthly', reference_period: '2026-04-01', deadline: '2026-06-14', freshness: 'ok' },
  },
}

const jurosResponse = {
  data: {
    selic_meta: [
      { data: '01/02/2026', valor: '13.75' },
      { data: '01/03/2026', valor: '14.00' },
      { data: '29/03/2026', valor: '14.25' },
    ],
  },
}

beforeEach(() => {
  window.history.pushState({}, '', '/credito')
  mockFetch.mockReset()
  mockFetch.mockImplementation(async input => {
    const url = input instanceof Request ? input.url : input.toString()
    if (url === '/api/credito') return Response.json(creditoResponse)
    if (url === '/api/juros') return Response.json(jurosResponse)
    if (url === '/api/status') return Response.json({ status: 'running' })
    return new Response(null, { status: 404 })
  })
  vi.stubGlobal('fetch', mockFetch)
})

afterEach(() => {
  window.history.pushState({}, '', '/')
  vi.unstubAllGlobals()
  vi.restoreAllMocks()
})

describe('Credito', () => {
  it('[RED XBR-202] renders five KPIs, three explicit comparisons, and non-causal trend guidance', async () => {
    render(<App />)

    const header = screen.getByRole('banner')
    expect(await within(header).findByText('Crédito e Famílias')).toBeInTheDocument()
    expect(screen.getByText('Inadimplência PF', { selector: '.kpi-label' })).toBeInTheDocument()
    expect(screen.getByText('Endividamento das famílias', { selector: '.kpi-label' })).toBeInTheDocument()
    expect(screen.getByText('Comprometimento de renda', { selector: '.kpi-label' })).toBeInTheDocument()
    expect(screen.getByText('ICC PF', { selector: '.kpi-label' })).toBeInTheDocument()
    expect(screen.getByText('Taxa média do crédito PF', { selector: '.kpi-label' })).toBeInTheDocument()
    expect(screen.getByText('Comprometimento de renda × inadimplência PF')).toBeInTheDocument()
    expect(screen.getByText('Selic meta × taxa média do crédito PF')).toBeInTheDocument()
    expect(screen.getByText('Endividamento × comprometimento de renda')).toBeInTheDocument()
    expect(screen.getByText(/mecanismo econômico geral, não como prova causal/i)).toBeInTheDocument()
    expect(screen.getByText(/Custo do crédito caiu/i)).toBeInTheDocument()
    expect(screen.getByText(/Comprometimento de renda caiu/i)).toBeInTheDocument()
    expect(screen.getByText(/Inadimplência PF subiu/i)).toBeInTheDocument()
    expect(screen.getByText('14,20% a.a.')).toBeInTheDocument()
    expect(screen.getByText('22,80% a.a.')).toBeInTheDocument()
    expect(screen.getByText(/última observação válida no mês/i)).toBeInTheDocument()
  })

  it('shows an accessible loading state while Crédito is pending', () => {
    mockFetch.mockImplementation(async input => {
      const url = input instanceof Request ? input.url : input.toString()
      if (url === '/api/credito') return new Promise<Response>(() => {})
      if (url === '/api/juros') return Response.json(jurosResponse)
      if (url === '/api/status') return Response.json({ status: 'running' })
      return new Response(null, { status: 404 })
    })

    render(<App />)

    expect(screen.getByRole('status')).toHaveTextContent('Carregando dados...')
  })

  it('reports that the Selic comparison is still loading while keeping Crédito visible', async () => {
    mockFetch.mockImplementation(async input => {
      const url = input instanceof Request ? input.url : input.toString()
      if (url === '/api/credito') return Response.json(creditoResponse)
      if (url === '/api/juros') return new Promise<Response>(() => {})
      if (url === '/api/status') return Response.json({ status: 'running' })
      return new Response(null, { status: 404 })
    })

    render(<App />)

    expect(await screen.findByText('Inadimplência PF', { selector: '.kpi-label' })).toBeInTheDocument()
    expect(screen.getByRole('status')).toHaveTextContent('Carregando Selic meta...')
  })

  it('makes a missing Selic source explicit without hiding available credit data', async () => {
    mockFetch.mockImplementation(async input => {
      const url = input instanceof Request ? input.url : input.toString()
      if (url === '/api/credito') return Response.json(creditoResponse)
      if (url === '/api/juros') return new Response(null, { status: 503 })
      if (url === '/api/status') return Response.json({ status: 'running' })
      return new Response(null, { status: 404 })
    })

    render(<App />)

    expect(await screen.findByText('Selic meta indisponível.')).toBeInTheDocument()
    expect(screen.getByRole('status')).toHaveTextContent('somente os dados de crédito disponíveis')
    expect(screen.getByText('Taxa média do crédito PF', { selector: '.kpi-label' })).toBeInTheDocument()
  })

  it('keeps available cards visible for partial and stale data without NaN', async () => {
    const partialResponse = {
      ...creditoResponse,
      status: 'partial',
      unavailable: ['taxa_media_credito_pf'],
      data: {
        ...creditoResponse.data,
        taxa_media_credito_pf: [],
      },
      metadata: {
        ...creditoResponse.metadata,
        comprometimento_renda: {
          ...creditoResponse.metadata.comprometimento_renda,
          freshness: 'stale',
        },
      },
    } as const
    mockFetch.mockImplementation(async input => {
      const url = input instanceof Request ? input.url : input.toString()
      if (url === '/api/credito') return Response.json(partialResponse)
      if (url === '/api/juros') return Response.json(jurosResponse)
      if (url === '/api/status') return Response.json({ status: 'running' })
      return new Response(null, { status: 404 })
    })

    render(<App />)

    expect(await screen.findByRole('status')).toHaveTextContent('Fonte parcialmente indisponível.')
    expect(screen.getByText('3,4%')).toBeInTheDocument()
    expect(screen.getByText('Taxa média do crédito PF', { selector: '.kpi-label' })).toBeInTheDocument()
    expect(screen.getAllByText('Indisponível').length).toBeGreaterThanOrEqual(1)
    expect(screen.queryByText(/NaN/)).not.toBeInTheDocument()
    expect(screen.getByText(/Custo do crédito: dados insuficientes/i)).toBeInTheDocument()
  })

  it('reports stale metadata while preserving the latest values', async () => {
    const staleResponse = {
      ...creditoResponse,
      metadata: {
        ...creditoResponse.metadata,
        comprometimento_renda: {
          ...creditoResponse.metadata.comprometimento_renda,
          freshness: 'stale',
        },
      },
    } as const
    mockFetch.mockImplementation(async input => {
      const url = input instanceof Request ? input.url : input.toString()
      if (url === '/api/credito') return Response.json(staleResponse)
      if (url === '/api/juros') return Response.json(jurosResponse)
      if (url === '/api/status') return Response.json({ status: 'running' })
      return new Response(null, { status: 404 })
    })

    render(<App />)

    expect(await screen.findByRole('status')).toHaveTextContent('Dados possivelmente desatualizados.')
    expect(screen.getByText('27,1%')).toBeInTheDocument()
  })

  it('renders the typed unavailable state for HTTP 503', async () => {
    mockFetch.mockImplementation(async input => {
      const url = input instanceof Request ? input.url : input.toString()
      if (url === '/api/credito') {
        return new Response('{"error":"dataset_unavailable","dataset":"credito"}', {
          status: 503,
          headers: { 'content-type': 'application/json' },
        })
      }
      if (url === '/api/juros') return Response.json(jurosResponse)
      if (url === '/api/status') return Response.json({ status: 'running' })
      return new Response(null, { status: 404 })
    })

    render(<App />)

    expect(await screen.findByRole('alert')).toHaveTextContent('Dados indisponíveis.')
    expect(screen.getByRole('alert')).toHaveTextContent('HTTP 503')
  })
})
