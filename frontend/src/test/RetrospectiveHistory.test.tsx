import { act, render, screen } from '@testing-library/react'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { RetrospectiveHistory } from '../components/macro-score/RetrospectiveHistory'

const fetchMock = vi.fn<typeof fetch>()

beforeEach(() => {
  fetchMock.mockReset()
  vi.stubGlobal('fetch', fetchMock)
})

afterEach(() => vi.unstubAllGlobals())

const response = (coverage: object) => Response.json({
  status: 'ok', source_basis: 'retrospective_reference', point_in_time: false,
  di_history_available: false, coverage,
})

describe('RetrospectiveHistory', () => {
  it('distinguishes 24-month eligible history from an insufficient series', async () => {
    fetchMock.mockResolvedValue(response({
      spread: { distinct_months: 24, percentile_eligible: true },
      focus: { distinct_months: 23, percentile_eligible: false },
    }))
    render(<RetrospectiveHistory />)
    expect(await screen.findByText(/spread: 24 meses distintos · elegível para percentil/)).toBeVisible()
    expect(screen.getByText(/focus: 23 meses distintos · histórico insuficiente/)).toBeVisible()
    expect(fetchMock).toHaveBeenCalledWith(
      '/api/macro-score/retrospective-history?start=2016-01&end=2026-12',
      expect.objectContaining({ signal: expect.any(AbortSignal) }),
    )
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
