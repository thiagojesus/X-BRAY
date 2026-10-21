import { fireEvent, render, screen, waitFor, within } from '@testing-library/react'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import Derivados from '../pages/Derivados'
import { derivedHistoryFixture } from './derivedHistoryFixtures'

const payload = {
  status: 'ok',
  data: {
    key: 'juro_real_ex_ante_1y', value: 6.841200076438003, unit: 'pct_annual',
    as_of: '2026-09-27T12:00:00+00:00', methodology_version: '1.0.0',
    formula: '((1 + DI/100) / (1 + IPCA/100) - 1) * 100', reference_date: '2026-09-25',
    historical_percentile: null, classification: null, classification_reason: 'insufficient_history', unavailable_reason: null,
    inputs: {
      expected_inflation: {
        value: 4.66, unit: 'pct_annual', reference_period: '2026-09-25', available_at: '2026-09-27T10:00:00+00:00', source: 'Focus IPCA 12m',
        metadata: { availability_basis: 'first_seen_at', filter: "Indicador eq 'IPCA' and Suavizada eq 'S' and baseCalculo eq 0" },
      },
      nominal_rate: {
        value: 11.82, unit: 'pct_annual', reference_period: '2026-09-25', available_at: '2026-09-27T10:00:00+00:00', source: 'B3 DI',
        metadata: { availability_basis: 'first_seen_at', symbol: 'DI1V27', maturity: '2027-09-25' },
      },
    },
  },
} as const

const mockFetch = vi.fn<typeof fetch>()
const neutralDisclosure = 'O resultado é uma medida derivada dos dados disponíveis no corte; não é uma previsão nem uma afirmação causal.'
let dedicatedResponses: readonly (() => Promise<Response>)[]
let dedicatedCalls: number

function setDedicatedResponses(...responses: readonly (() => Promise<Response>)[]): void {
  dedicatedResponses = responses
  dedicatedCalls = 0
}

beforeEach(() => {
  setDedicatedResponses(async () => Response.json(payload))
  mockFetch.mockReset().mockImplementation(async input => {
    const url = input instanceof Request ? input.url : input.toString()
    if (url === '/api/derivados') return Response.json({ status: 'unavailable', data: {} })
    if (url === '/api/derivados/history') return Response.json(derivedHistoryFixture)
    const response = dedicatedResponses[Math.min(dedicatedCalls++, dedicatedResponses.length - 1)]
    return response()
  })
  vi.stubGlobal('fetch', mockFetch)
})
afterEach(() => { vi.unstubAllGlobals(); vi.restoreAllMocks() })

describe('Derivados', () => {
  it('renders the available value, inputs, pending classification, and keyboard disclosure', async () => {
    render(<Derivados />)
    expect((await screen.findAllByText('6,84% a.a.')).length).toBeGreaterThanOrEqual(1)
    expect(screen.getByText('Classificação em formação.')).toBeInTheDocument()
    expect(screen.getByText('DI1V27')).toBeInTheDocument()
    const summary = screen.getByText('Como o indicador é calculado')
    expect(summary.closest('details')).not.toHaveAttribute('open')
    expect(screen.getByText(neutralDisclosure, { exact: true })).toBeVisible()
    summary.focus()
    fireEvent.keyDown(summary, { key: 'Enter' })
    fireEvent.click(summary)
    expect(summary.closest('details')).toHaveAttribute('open')
  })

  it('shows the reconstructed monthly history basis when classification is eligible', async () => {
    const eligiblePayload = {
      ...payload,
      data: {
        ...payload.data,
        historical_percentile: 37.5,
        classification: 'baixo',
        classification_reason: null,
        classification_history_basis: 'retrospective_reference_monthly_plus_production_forward_captured_monthly',
        classification_history_count: 33,
        classification_history_available_at: '2026-10-20T12:00:00+00:00',
      },
    }
    setDedicatedResponses(async () => Response.json(eligiblePayload))

    render(<Derivados />)

    expect(await screen.findByText('Baixo')).toBeInTheDocument()
    expect(screen.getByText('37,5º')).toBeInTheDocument()
    const historyNote = screen.getByText(/Histórico de classificação: 33 referências mensais elegíveis/)
    expect(historyNote).toHaveAttribute('role', 'note')
    expect(historyNote).toHaveTextContent(/Não é point-in-time/)
    expect(historyNote).toHaveTextContent(/Dados retrospectivos disponíveis até/)
    expect(screen.queryByText('Classificação em formação.')).not.toBeInTheDocument()
  })

  it('[RED XBR-502] identifies the primary metric as derived and renders frozen input provenance', async () => {
    // Given

    // When
    render(<Derivados />)

    // Then
    expect((await screen.findAllByText('6,84% a.a.')).length).toBeGreaterThanOrEqual(1)
    const focusSource = screen.getByText('Focus IPCA 12m')
    const nominalSource = screen.getByText('B3 DI')
    expect(focusSource).toBeInTheDocument()
    expect(nominalSource).toBeInTheDocument()
    expect(screen.getByText('DI1V27')).toBeInTheDocument()

    const hero = screen.getByRole('region', { name: 'Juro real ex-ante de um ano' })
    const primaryMetric = within(hero).getByText('6,84% a.a.').parentElement
    const expectedInflationCard = focusSource.closest('article')
    const nominalRateCard = nominalSource.closest('article')
    expect(primaryMetric).not.toBeNull()
    expect(expectedInflationCard).not.toBeNull()
    expect(nominalRateCard).not.toBeNull()
    if (!(primaryMetric instanceof HTMLElement) || !(expectedInflationCard instanceof HTMLElement) || !(nominalRateCard instanceof HTMLElement)) {
      throw new Error('Expected the primary metric and both input cards to render')
    }

    expect.soft(within(primaryMetric).queryByText('Medida derivada')).toBeInTheDocument()
    expect.soft(within(expectedInflationCard).queryByText("Indicador eq 'IPCA' and Suavizada eq 'S' and baseCalculo eq 0")).toBeInTheDocument()
    expect.soft(within(expectedInflationCard).queryByText('first_seen_at')).toBeInTheDocument()
    expect.soft(within(nominalRateCard).queryByText('first_seen_at')).toBeInTheDocument()
  })

  it('renders the exact empty-cache unavailable state', async () => {
    setDedicatedResponses(async () => Response.json({ status: 'unavailable', data: {} }))
    render(<Derivados />)
    expect(await screen.findByText('Indicador indisponível.')).toBeInTheDocument()
    expect(screen.getByText('O primeiro snapshot elegível ainda não foi calculado.')).toBeInTheDocument()
    expect(screen.queryByText('Pendente')).not.toBeInTheDocument()
    expect(screen.getByText('Como o indicador é calculado').closest('details')).not.toHaveAttribute('open')
    expect(screen.getByText(neutralDisclosure, { exact: true })).toBeVisible()
  })

  it('keeps the last value visible when a retry fails', async () => {
    setDedicatedResponses(
      async () => Response.json(payload),
      async () => Promise.reject(new TypeError('offline')),
    )
    render(<Derivados />)
    expect((await screen.findAllByText('6,84% a.a.')).length).toBeGreaterThanOrEqual(1)
    window.dispatchEvent(new Event('online'))
    await waitFor(() => expect(screen.getByText('Dados possivelmente desatualizados.')).toBeInTheDocument())
    expect(screen.getAllByText('6,84% a.a.').length).toBeGreaterThanOrEqual(1)
  })

  it('shows a typed loading and HTTP failure state', async () => {
    setDedicatedResponses(async () => new Response(null, { status: 503 }))
    render(<Derivados />)
    expect(screen.getByText('Carregando dados...')).toBeInTheDocument()
    expect(screen.queryByText(neutralDisclosure, { exact: true })).not.toBeInTheDocument()
    expect(await screen.findByRole('alert')).toHaveTextContent('HTTP 503')
    expect(screen.getByText(neutralDisclosure, { exact: true })).toBeVisible()
    expect(screen.queryByText('Como o indicador é calculado')).not.toBeInTheDocument()
    expect(screen.queryByRole('article')).not.toBeInTheDocument()
  })
})
