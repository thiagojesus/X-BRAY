import { act, fireEvent, render, screen, waitFor, within } from '@testing-library/react'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { MacroScore } from '../pages/MacroScore'
import { macroScoreFixture, macroScoreHistoryFixture, partialScoreFixture, readySnapshot, unavailableScoreFixture } from './macroScoreFixtures'

const mockFetch = vi.fn<typeof fetch>()
let latestResponse: () => Promise<Response>
let historyResponse: () => Promise<Response>

beforeEach(() => {
  latestResponse = async () => Response.json(macroScoreFixture)
  historyResponse = async () => Response.json(macroScoreHistoryFixture)
  mockFetch.mockReset().mockImplementation(input => {
    const url = input instanceof Request ? input.url : input.toString()
    return url.startsWith('/api/macro-score/history?') ? historyResponse() : latestResponse()
  })
  vi.stubGlobal('fetch', mockFetch)
})
afterEach(() => { vi.useRealTimers(); vi.unstubAllGlobals(); vi.restoreAllMocks() })

describe('Macro Score persisted page', () => {
  it('renders the server score rather than the dimension average when coverage is complete', async () => {
    // Given
    // When
    render(<MacroScore />)
    // Then
    expect(await screen.findByTestId('macro-score-value')).toHaveTextContent(/^73,125$/)
    expect(screen.getByText('Forte', { exact: true })).toBeVisible()
    expect(screen.getByText('Beta · 0.1.0-beta')).toBeVisible()
    const overview = screen.getByRole('region', { name: 'Score geral' })
    expect(within(overview).getByText('pontos (0 a 100)', { exact: true })).toBeVisible()
    expect(overview).toHaveTextContent('2026-09')
    expect(overview).toHaveTextContent('6/6 · 100%')
    expect(overview).toHaveTextContent('30/09/2026, 12:00:00 UTC')
    expect(screen.getAllByRole('article')).toHaveLength(6)
    expect(screen.getByText(/Cobertura completa das dimensões/)).toBeVisible()
    await screen.findByText('Histórico persistido')
    expect(mockFetch).toHaveBeenCalledWith('/api/macro-score/history?start=2016-10&end=2026-09', expect.objectContaining({ signal: expect.any(AbortSignal) }))
    expect(mockFetch.mock.calls.some(([url]) => String(url).includes('/summary'))).toBe(false)
    expect(mockFetch.mock.calls.some(([url]) => String(url).includes('/data-quality'))).toBe(false)
  })

  it('exposes partial coverage and persisted warnings when five dimensions are valid', async () => {
    // Given
    latestResponse = async () => Response.json(partialScoreFixture)
    // When
    render(<MacroScore />)
    // Then
    expect(await screen.findByTestId('macro-score-value')).toHaveTextContent('69,875')
    expect(screen.getByText('5/6 · 83,33%')).toBeVisible()
    expect(screen.getByText(/Cobertura parcial das dimensões/)).toBeVisible()
    expect(screen.getByText('monetary:insufficient_coverage')).toBeVisible()
    const monetary = screen.getByRole('article', { name: 'Monetária' })
    expect(monetary).toHaveTextContent('1/2 componentes · 50%')
    expect(monetary).toHaveTextContent('Cobertura insuficiente (insufficient_coverage)')
  })

  it('retains dimensions and provenance without a numeric placeholder when a null-score snapshot exists', async () => {
    // Given
    latestResponse = async () => Response.json(unavailableScoreFixture)
    // When
    render(<MacroScore />)
    // Then
    expect(await screen.findByTestId('macro-score-value')).toHaveTextContent(/^Indisponível$/)
    expect(screen.getByText('4/6 · 66,67%')).toBeVisible()
    expect(screen.getByText(/Score indisponível com snapshot persistido/)).toBeVisible()
    expect(screen.getAllByRole('article')).toHaveLength(6)
    expect(screen.queryByText(/Sem snapshot persistido\./)).not.toBeInTheDocument()
  })

  it('uses the Sao Paulo calendar month when no latest snapshot exists', async () => {
    // Given
    vi.useFakeTimers({ toFake: ['Date'] })
    vi.setSystemTime(new Date('2026-10-01T01:00:00Z'))
    latestResponse = async () => Response.json({ status: 'unavailable', data: null })
    historyResponse = async () => Response.json({ status: 'unavailable', data: [] })
    // When
    render(<MacroScore />)
    // Then
    expect(await screen.findByText(/Sem snapshot persistido\./)).toBeVisible()
    expect(await screen.findByText(/Histórico vazio:/)).toBeVisible()
    expect(screen.queryByTestId('macro-score-value')).not.toBeInTheDocument()
    expect(screen.queryByRole('article')).not.toBeInTheDocument()
    expect(mockFetch).toHaveBeenCalledWith('/api/macro-score/history?start=2016-10&end=2026-09', expect.anything())
  })

  it('keeps methodology reachable without fabricated cards when latest is loading', () => {
    // Given
    latestResponse = () => new Promise<Response>(() => {})
    // When
    render(<MacroScore />)
    // Then
    expect(screen.getByRole('status')).toHaveTextContent('Carregando Macro Score...')
    expect(screen.getByText('Metodologia Macro Score Beta')).toBeVisible()
    expect(screen.queryByRole('article')).not.toBeInTheDocument()
    expect(screen.queryByText('Histórico persistido')).not.toBeInTheDocument()
  })

  it.each([422, 503])('keeps HTTP %i distinct from no snapshot', async status => {
    // Given
    latestResponse = async () => new Response(null, { status })
    // When
    render(<MacroScore />)
    // Then
    expect(await screen.findByRole('alert')).toHaveTextContent(`Falha de transporte: HTTP ${status}`)
    expect(screen.getByText('Metodologia Macro Score Beta')).toBeVisible()
    expect(screen.queryByText(/Sem snapshot persistido\./)).not.toBeInTheDocument()
    expect(screen.queryByTestId('macro-score-value')).not.toBeInTheDocument()
  })

  it('rejects malformed JSON rather than labeling it empty', async () => {
    // Given
    latestResponse = async () => new Response('{broken', { headers: { 'Content-Type': 'application/json' } })
    // When
    render(<MacroScore />)
    // Then
    expect(await screen.findByRole('alert')).toHaveTextContent('Resposta inválida da API; dados rejeitados.')
    expect(screen.queryByText(/Sem snapshot persistido\./)).not.toBeInTheDocument()
  })

  it('rejects a malformed payload before any persisted fields render', async () => {
    // Given
    latestResponse = async () => Response.json({ status: 'ok', data: { ...readySnapshot, score: '73.125' } })
    // When
    render(<MacroScore />)
    // Then
    expect(await screen.findByRole('alert')).toHaveTextContent('Resposta inválida da API')
    expect(screen.queryByTestId('macro-score-value')).not.toBeInTheDocument()
  })

  it('preserves validated cached values on a failed explicit retry', async () => {
    // Given
    render(<MacroScore />)
    await screen.findByTestId('macro-score-value')
    latestResponse = async () => { throw new TypeError('offline') }
    // When
    fireEvent.click(screen.getByRole('button', { name: 'Tentar Macro Score novamente' }))
    // Then
    expect(await screen.findByText(/Snapshot em cache possivelmente desatualizado/)).toBeVisible()
    expect(screen.getByRole('alert')).toHaveTextContent('offline')
    expect(screen.getByTestId('macro-score-value')).toHaveTextContent('73,125')
    expect(screen.getByText('6/6 · 100%')).toBeVisible()
  })

  it('preserves parsed cache when a retry returns an invalid readiness envelope', async () => {
    // Given
    render(<MacroScore />)
    await screen.findByTestId('macro-score-value')
    latestResponse = async () => Response.json({ status: 'ok', data: null })
    // When
    fireEvent.click(screen.getByRole('button', { name: 'Tentar Macro Score novamente' }))
    // Then
    expect(await screen.findByRole('alert')).toHaveTextContent('Resposta inválida da API')
    expect(screen.getByTestId('macro-score-value')).toHaveTextContent('73,125')
    expect(screen.getByText(/Snapshot em cache/)).toBeVisible()
  })

  it('reports loading history independently when latest is ready', async () => {
    // Given
    historyResponse = () => new Promise<Response>(() => {})
    // When
    render(<MacroScore />)
    // Then
    expect(await screen.findByText('Carregando histórico...')).toBeVisible()
    expect(screen.getByTestId('macro-score-value')).toHaveTextContent('73,125')
  })

  it.each([422, 503])('reports history HTTP %i independently without claiming empty history', async status => {
    // Given
    historyResponse = async () => new Response(null, { status })
    // When
    render(<MacroScore />)
    // Then
    expect(await screen.findByRole('alert')).toHaveTextContent(`Erro no histórico: Falha de transporte: HTTP ${status}`)
    expect(screen.getByTestId('macro-score-value')).toHaveTextContent('73,125')
    expect(screen.queryByText(/Histórico vazio:/)).not.toBeInTheDocument()
  })

  it('keeps cached history visible after an online retry fails', async () => {
    // Given
    render(<MacroScore />)
    await screen.findByText('Valores acessíveis: Score geral')
    historyResponse = async () => { throw new TypeError('offline') }
    // When
    act(() => window.dispatchEvent(new Event('online')))
    // Then
    expect(await screen.findByText(/Histórico em cache possivelmente desatualizado/)).toBeVisible()
    expect(screen.getByText('Valores acessíveis: Score geral')).toBeVisible()
  })

  it('discards an aborted response when a second request wins', async () => {
    // Given
    let release: ((response: Response) => void) | undefined
    latestResponse = () => new Promise<Response>(resolve => { release = resolve })
    render(<MacroScore />)
    await waitFor(() => expect(mockFetch).toHaveBeenCalledTimes(1))
    latestResponse = async () => Response.json(partialScoreFixture)
    // When
    act(() => window.dispatchEvent(new Event('online')))
    await screen.findByTestId('macro-score-value')
    await act(async () => { release?.(Response.json(macroScoreFixture)); await Promise.resolve() })
    // Then
    expect(screen.getByTestId('macro-score-value')).toHaveTextContent('69,875')
    expect(within(screen.getByRole('region', { name: 'Score geral' })).getByText('5/6 · 83,33%')).toBeVisible()
  })
})
