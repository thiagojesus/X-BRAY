import { act, render, screen } from '@testing-library/react'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { RetrospectiveHistory } from '../components/macro-score/RetrospectiveHistory'

const fetchMock = vi.fn<typeof fetch>()

beforeEach(() => {
  fetchMock.mockReset()
  vi.stubGlobal('fetch', fetchMock)
})

afterEach(() => { vi.useRealTimers(); vi.unstubAllGlobals() })

const response = (coverage: object, diAvailable = false) => Response.json({
  status: 'ok', source_basis: 'retrospective_reference', point_in_time: false,
  di_history_available: diAvailable, coverage,
})

describe('RetrospectiveHistory', () => {
  it('requests the inclusive 120-month window ending in the current Sao Paulo month', async () => {
    vi.useFakeTimers({ toFake: ['Date'] })
    vi.setSystemTime(new Date('2026-10-21T04:00:00Z'))
    fetchMock.mockResolvedValue(response({}))
    render(<RetrospectiveHistory />)
    await screen.findByText(/DI: sem histórico retrospectivo/)
    expect(fetchMock).toHaveBeenCalledWith(
      '/api/macro-score/retrospective-history?start=2016-11&end=2026-10',
      expect.objectContaining({ signal: expect.any(AbortSignal) }),
    )
    vi.useRealTimers()
  })

  it('separates candidate months from percentile eligibility and describes direct DI expiry evidence', async () => {
    vi.useFakeTimers({ toFake: ['Date'] })
    vi.setSystemTime(new Date('2026-10-21T04:00:00Z'))
    fetchMock.mockResolvedValue(response({
      spread: { distinct_months: 24, eligible_distinct_months: 24, percentile_eligible: true },
      focus: { distinct_months: 23, percentile_eligible: false },
      juro_real_ex_ante_1y: { distinct_months: 34, eligible_distinct_months: 0, percentile_eligible: false },
    }, true))
    render(<RetrospectiveHistory />)
    expect(await screen.findByText(/spread: 24 meses candidatos · 24 elegíveis · elegível para percentil/)).toBeVisible()
    expect(screen.getByText(/focus: 23 meses candidatos · não elegível para percentil/)).toBeVisible()
    expect(screen.getByText(/juro_real_ex_ante_1y: 34 meses candidatos · 0 elegíveis · não elegível para percentil/)).toBeVisible()
    expect(screen.getByText(/vencimento é confrontado com o cadastro de instrumentos DI1 \(XprtnDt\), separado do último dia de negociação \(TradgEndDt\)/)).toBeVisible()
    expect(fetchMock).toHaveBeenCalledWith(
      '/api/macro-score/retrospective-history?start=2016-11&end=2026-10',
      expect.objectContaining({ signal: expect.any(AbortSignal) }),
    )
    vi.useRealTimers()
  })

  it('reports transport, malformed response, and non-Error failures without crashing', async () => {
    fetchMock.mockResolvedValueOnce(new Response(null, { status: 503 }))
    const first = render(<RetrospectiveHistory />)
    expect(await screen.findByText('Histórico retrospectivo indisponível.')).toBeVisible()
    first.unmount()

    fetchMock.mockResolvedValueOnce(Response.json({ status: 'ok', coverage: {} }))
    const second = render(<RetrospectiveHistory />)
    expect(await screen.findByText('Resposta de histórico retrospectivo inválida.')).toBeVisible()
    second.unmount()

    fetchMock.mockRejectedValueOnce('offline')
    render(<RetrospectiveHistory />)
    expect(await screen.findByText('Falha ao consultar histórico retrospectivo.')).toBeVisible()
  })

  it('ignores a rejected request after unmount aborts the fetch', async () => {
    let rejectRequest: ((reason: unknown) => void) | undefined
    fetchMock.mockReturnValue(new Promise<Response>((_resolve, reject) => { rejectRequest = reject }))
    const { unmount } = render(<RetrospectiveHistory />)
    unmount()
    await act(async () => { rejectRequest?.(new Error('aborted')) })
    expect(screen.queryByText('aborted')).not.toBeInTheDocument()
  })
})
