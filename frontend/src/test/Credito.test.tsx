import { fireEvent, render, screen, within } from '@testing-library/react'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'

import App from '../App'
import { syntheticModalities } from './creditoModalities'

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
    carteira_pf_cheque_especial: [{ data: '01/01/2026', valor: '9999' }],
    carteira_pf_credito_pessoal_nao_consignado: [{ data: '01/01/2026', valor: '9999' }],
    carteira_pf_cartao_rotativo: [{ data: '01/01/2026', valor: '9999' }],
    carteira_pf_cartao_parcelado: [{ data: '01/01/2026', valor: '9999' }],
  },
  metadata: {
    inadimplencia_pf: { code: 21084, unit: 'pct', frequency: 'monthly', reference_period: '2026-03-01', deadline: '2026-05-15', freshness: 'ok' },
    endividamento_familias: { code: 29037, unit: 'pct', frequency: 'monthly', reference_period: '2026-03-01', deadline: '2026-06-14', freshness: 'ok' },
    comprometimento_renda: { code: 29034, unit: 'pct', frequency: 'monthly', reference_period: '2026-03-01', deadline: '2026-06-14', freshness: 'ok' },
    icc_pf: { code: 25353, unit: 'pct_annual', frequency: 'monthly', reference_period: '2026-03-01', deadline: '2026-05-15', freshness: 'ok' },
    taxa_media_credito_pf: { code: 20716, unit: 'pct_annual', frequency: 'monthly', reference_period: '2026-04-01', deadline: '2026-06-14', freshness: 'ok' },
    carteira_pf_cheque_especial: { code: 20573, unit: 'brl_million', frequency: 'monthly', reference_period: '2026-01-01', deadline: '2026-03-17', freshness: 'ok' },
    carteira_pf_credito_pessoal_nao_consignado: { code: 20574, unit: 'brl_million', frequency: 'monthly', reference_period: '2026-01-01', deadline: '2026-03-17', freshness: 'ok' },
    carteira_pf_cartao_rotativo: { code: 20587, unit: 'brl_million', frequency: 'monthly', reference_period: '2026-01-01', deadline: '2026-03-17', freshness: 'ok' },
    carteira_pf_cartao_parcelado: { code: 20588, unit: 'brl_million', frequency: 'monthly', reference_period: '2026-01-01', deadline: '2026-03-17', freshness: 'ok' },
  },
  modalities: syntheticModalities,
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
    expect(screen.getByRole('heading', { name: 'Qualidade e estresse do crédito PF' })).toBeInTheDocument()
    expect(screen.getByText('R$ 120,5 mi')).toBeInTheDocument()
    expect(screen.getByText('9%')).toBeInTheDocument()
    expect(screen.getByText('19,9%')).toBeInTheDocument()
    expect(screen.getByText('Crédito pessoal não consignado total')).toBeInTheDocument()
    expect(screen.getByText('Cartão parcelado (operações com juros)')).toBeInTheDocument()
    expect(screen.getAllByRole('columnheader').length).toBe(4)
    expect(document.querySelectorAll('.chart-container')).toHaveLength(5)
    expect(screen.getAllByText(/Mês-base esperado: 12\/2025/)).toHaveLength(4)
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

  it('isolates a null modality, shows its explicit reason, and keeps stale siblings active', async () => {
    const partial = {
      ...creditoResponse,
      status: 'partial',
      modalities: {
        ...syntheticModalities,
        carteira_pf_cheque_especial: null,
        carteira_pf_cartao_rotativo: {
          ...syntheticModalities.carteira_pf_cartao_rotativo,
          latest: { reference: null, value: null, reason: 'no_valid_observations' as const },
          unavailable: true,
        },
      },
      metadata: {
        ...creditoResponse.metadata,
        carteira_pf_cartao_parcelado: {
          ...creditoResponse.metadata.carteira_pf_cartao_parcelado,
          freshness: 'stale',
        },
      },
    }
    mockFetch.mockImplementation(async input => {
      const url = input instanceof Request ? input.url : input.toString()
      if (url === '/api/credito') return Response.json(partial)
      if (url === '/api/juros') return Response.json(jurosResponse)
      if (url === '/api/status') return Response.json({ status: 'running' })
      return new Response(null, { status: 404 })
    })

    render(<App />)

    expect(await screen.findByText('Modalidade indisponível.')).toBeInTheDocument()
    expect(screen.getByText('Sem observações válidas em cache.')).toBeInTheDocument()
    expect(screen.getByText('Referência possivelmente desatualizada.')).toBeInTheDocument()
    expect(screen.getByText('R$ 575 mi')).toBeInTheDocument()
    const scrollRegion = screen.getByRole('region', { name: 'Tabela das modalidades de crédito PF' })
    const scrollBy = vi.fn()
    Object.defineProperty(scrollRegion, 'scrollBy', { value: scrollBy })
    fireEvent.keyDown(scrollRegion, { key: 'ArrowRight' })
    fireEvent.keyDown(scrollRegion, { key: 'ArrowLeft' })
    expect(scrollBy).toHaveBeenNthCalledWith(1, { left: 48 })
    expect(scrollBy).toHaveBeenNthCalledWith(2, { left: -48 })
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

  it('keeps legacy KPIs and charts usable with an older response that omits modalities', async () => {
    const { modalities: _modalities, ...legacyResponse } = creditoResponse
    mockFetch.mockImplementation(async input => {
      const url = input instanceof Request ? input.url : input.toString()
      if (url === '/api/credito') return Response.json(legacyResponse)
      if (url === '/api/juros') return Response.json(jurosResponse)
      if (url === '/api/status') return Response.json({ status: 'running' })
      return new Response(null, { status: 404 })
    })
    render(<App />)
    expect(await screen.findByText('Inadimplência PF', { selector: '.kpi-label' })).toBeInTheDocument()
    expect(screen.queryByText('Crédito pessoal não consignado total')).not.toBeInTheDocument()
    expect(screen.getByRole('status')).toHaveTextContent('Dados das modalidades de crédito não vieram')
    expect(document.querySelectorAll('.chart-container')).toHaveLength(3)
  })

  it('shows per-modality unavailable and stale states without hiding sibling exposures', async () => {
    const { carteira_pf_cartao_rotativo: _missing, ...modalities } = syntheticModalities
    const partialResponse = {
      ...creditoResponse,
      modalities: {
        ...modalities,
        carteira_pf_cheque_especial: {
          ...syntheticModalities.carteira_pf_cheque_especial,
          latest: { reference: '2026-01-01', value: 0, reason: null },
        },
      },
      metadata: {
        ...creditoResponse.metadata,
        carteira_pf_cartao_parcelado: {
          ...creditoResponse.metadata.carteira_pf_cartao_parcelado,
          freshness: 'stale',
        },
      },
    }
    mockFetch.mockImplementation(async input => {
      const url = input instanceof Request ? input.url : input.toString()
      if (url === '/api/credito') return Response.json(partialResponse)
      if (url === '/api/juros') return Response.json(jurosResponse)
      if (url === '/api/status') return Response.json({ status: 'running' })
      return new Response(null, { status: 404 })
    })
    render(<App />)
    expect(await screen.findByText('R$ 0 mi')).toBeInTheDocument()
    expect(screen.getAllByText('Cartão rotativo').length).toBeGreaterThan(0)
    expect(screen.getByText('Referência possivelmente desatualizada.')).toBeInTheDocument()
    expect(screen.getAllByText('Indisponível').length).toBeGreaterThan(0)
    expect(screen.queryByText(/NaN|Infinity/)).not.toBeInTheDocument()
  })

  it('renders exposure-only data while old risk indicators remain unavailable', async () => {
    const exposureOnly = {
      ...creditoResponse,
      data: Object.fromEntries(Object.entries(creditoResponse.data).filter(([key]) => key.startsWith('carteira_pf_'))),
      unavailable: ['inadimplencia_pf', 'endividamento_familias', 'comprometimento_renda', 'icc_pf', 'taxa_media_credito_pf'],
      status: 'partial',
    }
    mockFetch.mockImplementation(async input => {
      const url = input instanceof Request ? input.url : input.toString()
      if (url === '/api/credito') return Response.json(exposureOnly)
      if (url === '/api/juros') return Response.json(jurosResponse)
      if (url === '/api/status') return Response.json({ status: 'running' })
      return new Response(null, { status: 404 })
    })
    render(<App />)
    expect(await screen.findByText('R$ 120,5 mi')).toBeInTheDocument()
    expect(screen.getAllByText('Indisponível', { selector: '.kpi-value' })).toHaveLength(5)
    expect(screen.getByText('Saldos nominais das modalidades selecionadas')).toBeInTheDocument()
  })

  it('explains zero-base, invalid-base, and absent observations without treating them as zero growth', async () => {
    const edgeModalities = {
      ...syntheticModalities,
      carteira_pf_cheque_especial: {
        ...syntheticModalities.carteira_pf_cheque_especial,
        latest: { reference: 'bad-reference', value: null, reason: 'no_valid_observations' as const },
        unavailable: true,
        mom: { ...syntheticModalities.carteira_pf_cheque_especial.mom, value_pct: null, reason: 'zero_base' as const, base_reference: '2025-12-01' },
        yoy: { ...syntheticModalities.carteira_pf_cheque_especial.yoy, value_pct: null, reason: 'invalid_base' as const, base_reference: null },
      },
      carteira_pf_credito_pessoal_nao_consignado: {
        ...syntheticModalities.carteira_pf_credito_pessoal_nao_consignado,
        latest: { reference: null, value: 0, reason: null },
        unavailable: false,
        mom: { ...syntheticModalities.carteira_pf_credito_pessoal_nao_consignado.mom, value_pct: null, reason: 'invalid_result' as const, base_reference: null },
      },
      carteira_pf_cartao_rotativo: {
        ...syntheticModalities.carteira_pf_cartao_rotativo,
        latest: { reference: null, value: null, reason: 'invalid_current' as const },
        unavailable: true,
        mom: { ...syntheticModalities.carteira_pf_cartao_rotativo.mom, value_pct: null, reason: 'missing_current' as const, base_reference: null },
      },
    }
    const edgeResponse = { ...creditoResponse, modalities: edgeModalities }
    mockFetch.mockImplementation(async input => {
      const url = input instanceof Request ? input.url : input.toString()
      if (url === '/api/credito') return Response.json(edgeResponse)
      if (url === '/api/juros') return Response.json(jurosResponse)
      if (url === '/api/status') return Response.json({ status: 'running' })
      return new Response(null, { status: 404 })
    })
    render(<App />)
    expect(await screen.findByText('Variação indefinida: saldo do mês-base igual a zero. Mês-base esperado: 12/2025.')).toBeInTheDocument()
    expect(screen.getByText('Mês-base contém observação inválida.')).toBeInTheDocument()
    expect(screen.getByText('Variação não finita; cálculo indisponível.')).toBeInTheDocument()
    expect(screen.getByText('Sem saldo válido neste mês.')).toBeInTheDocument()
    expect(screen.getByText('R$ 0 mi')).toBeInTheDocument()
    expect(screen.getByText('Ref. bad-reference')).toBeInTheDocument()
    expect(screen.queryByText(/NaN|Infinity/)).not.toBeInTheDocument()
  })

  it('reports an all-null modality block as unavailable while retaining legacy charts', async () => {
    const noneAvailable = { ...creditoResponse, modalities: {
      carteira_pf_cheque_especial: null,
      carteira_pf_credito_pessoal_nao_consignado: null,
      carteira_pf_cartao_rotativo: null,
      carteira_pf_cartao_parcelado: null,
    } }
    mockFetch.mockImplementation(async input => {
      const url = input instanceof Request ? input.url : input.toString()
      if (url === '/api/credito') return Response.json(noneAvailable)
      if (url === '/api/juros') return Response.json(jurosResponse)
      if (url === '/api/status') return Response.json({ status: 'running' })
      return new Response(null, { status: 404 })
    })
    render(<App />)
    expect(await screen.findByText('Dados das modalidades de crédito não vieram nesta resposta. Os indicadores e gráficos de crédito disponíveis continuam exibidos.')).toBeInTheDocument()
    expect(document.querySelectorAll('.chart-container')).toHaveLength(3)
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
