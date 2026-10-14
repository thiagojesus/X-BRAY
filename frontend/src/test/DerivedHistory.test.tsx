import { act, fireEvent, render, screen } from '@testing-library/react'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { DerivedHistory } from '../components/derived-history/DerivedHistory'
import { buildDerivedHistoryRows, formatDerivedHistoryCutoff } from '../components/derived-history/historyUtils'
import { derivedHistoryFixture } from './derivedHistoryFixtures'

vi.mock('../components/derived-history/DerivedHistoryChart', () => ({
  DerivedHistoryChart: () => <div data-testid="derived-history-chart" />,
}))

const fetchMock = vi.fn<typeof fetch>()

beforeEach(() => {
  fetchMock.mockReset().mockResolvedValue(Response.json(derivedHistoryFixture))
  vi.stubGlobal('fetch', fetchMock)
})

afterEach(() => vi.unstubAllGlobals())

describe('derived indicator history', () => {
  it('retains the full persisted timestamp in a readable UTC cutoff label', () => {
    const cutoff = formatDerivedHistoryCutoff('2026-06-30T14:00:12.345678+00:00')
    expect(cutoff).toContain('14:00:12 UTC')
    expect(cutoff).toContain('(as_of: 2026-06-30T14:00:12.345678+00:00)')
  })

  it('merges the 24-month calendar and preserves missing snapshots separately from unavailable values', () => {
    const rows = buildDerivedHistoryRows(derivedHistoryFixture)
    expect(rows).toHaveLength(24)
    expect(rows[0]?.month).toBe('2024-11')
    expect(rows[23]?.month).toBe('2026-10')
    expect(rows.find(row => row.month === '2026-07')?.points.spread_credito_pf_selic).toBeUndefined()
    expect(rows.find(row => row.month === '2026-08')?.points.variacao_saldo_primario_12m?.snapshot.value).toBeNull()
  })

  it('loads only persisted history and groups the selected series by a single unit', async () => {
    render(<DerivedHistory />)
    expect(await screen.findByTestId('derived-history-chart')).toBeVisible()
    expect(fetchMock).toHaveBeenCalledWith('/api/derivados/history', expect.objectContaining({ signal: expect.any(AbortSignal) }))
    expect(screen.getByRole('checkbox', { name: /Spread de crédito PF sobre Selic/ })).toBeChecked()

    fireEvent.click(screen.getByRole('button', { name: 'Taxas anuais (% a.a.)' }))
    expect(await screen.findByRole('checkbox', { name: /Juro real ex-ante de um ano/ })).toBeDisabled()
    expect(screen.queryByTestId('derived-history-chart')).not.toBeInTheDocument()
  })

  it('distinguishes an empty persisted window from transport failure', async () => {
    fetchMock.mockResolvedValueOnce(Response.json({
      ...derivedHistoryFixture,
      status: 'unavailable',
      data: Object.fromEntries(Object.keys(derivedHistoryFixture.data).map(key => [key, []])),
    }))
    render(<DerivedHistory />)
    expect(await screen.findByText('Nenhum snapshot de produção persistido nesta janela.')).toBeVisible()
    expect(screen.queryByTestId('derived-history-chart')).not.toBeInTheDocument()
  })

  it('rejects a response whose point does not match its series before rendering values', async () => {
    const malformed = structuredClone(derivedHistoryFixture)
    const point = malformed.data.spread_credito_pf_selic[0]
    expect(point).toBeDefined()
    if (point !== undefined) point.snapshot.key = 'inclinacao_di_5y_1y'
    fetchMock.mockResolvedValueOnce(Response.json(malformed))
    render(<DerivedHistory />)
    expect(await screen.findByRole('alert')).toHaveTextContent('Resposta inválida da API; dados rejeitados.')
    expect(screen.queryByTestId('derived-history-chart')).not.toBeInTheDocument()
  })

  it('keeps the last valid chart data visible after an online retry fails', async () => {
    render(<DerivedHistory />)
    expect(await screen.findByTestId('derived-history-chart')).toBeVisible()
    fetchMock.mockRejectedValueOnce(new TypeError('offline'))
    await act(async () => { window.dispatchEvent(new Event('online')) })
    expect(await screen.findByRole('alert')).toHaveTextContent('offline')
    expect(screen.getByText('Exibindo a última resposta válida; ela pode estar desatualizada.')).toBeVisible()
    expect(screen.getByTestId('derived-history-chart')).toBeVisible()
  })
})
