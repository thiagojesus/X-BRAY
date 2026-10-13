import { render, screen, waitFor, within } from '@testing-library/react'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import App from '../App'
import { macroScoreFixture } from './macroScoreFixtures'

beforeEach(() => {
  vi.stubGlobal('fetch', vi.fn().mockResolvedValue({ ok: true, json: () => Promise.resolve({ status: 'running', cache: {} }) }))
})
afterEach(() => vi.restoreAllMocks())

describe('App', () => {
  it('renders without crashing', () => {
    render(<App />)
    expect(screen.getAllByAltText('X-BRAY').length).toBeGreaterThanOrEqual(1)
  })

  it('renders dashboard by default', () => {
    render(<App />)
    expect(screen.getByText('Raio-X do Macro Brasileiro')).toBeInTheDocument()
  })

  it('renders all nav sections', () => {
    render(<App />)
    const nav = screen.getByRole('navigation')
    expect(within(nav).getByText('Taxas de Juros')).toBeInTheDocument()
    expect(within(nav).getByText('Inflação')).toBeInTheDocument()
    expect(within(nav).getByText('IPCA Decomposição')).toBeInTheDocument()
    expect(within(nav).getByText('Atividade Econômica')).toBeInTheDocument()
    expect(within(nav).getByText('Fiscal')).toBeInTheDocument()
    expect(within(nav).getByText('Crédito e Famílias')).toBeInTheDocument()
    expect(within(nav).getByText('Setor Externo')).toBeInTheDocument()
    expect(within(nav).getByText('Câmbio')).toBeInTheDocument()
    expect(within(nav).getByText('IMA-B')).toBeInTheDocument()
    expect(within(nav).getByText('Tesouro Direto')).toBeInTheDocument()
    expect(within(nav).getByText('Expectativas FOCUS')).toBeInTheDocument()
    expect(within(nav).getByText('Complementares')).toBeInTheDocument()
    expect(within(nav).getByText('Indicadores Derivados')).toBeInTheDocument()
    expect(within(nav).getByText('Macro Score Beta')).toBeInTheDocument()
    expect(within(nav).queryByText('Eleições')).not.toBeInTheDocument()
  })

  it('redirects the deprecated elections route to the dashboard', async () => {
    window.history.pushState({}, '', '/eleicoes')

    render(<App />)

    await waitFor(() => expect(window.location.pathname).toBe('/'))
    expect(screen.getByText('Raio-X do Macro Brasileiro')).toBeInTheDocument()
  })

  it('renders Macro Score at its route when a persisted API snapshot is available', async () => {
    // Given
    vi.stubGlobal('fetch', vi.fn<typeof fetch>().mockImplementation(async input => {
      const url = String(input)
      if (url === '/api/macro-score') return Response.json(macroScoreFixture)
      if (url.includes('/history?')) return Response.json({ status: 'unavailable', data: [] })
      return Response.json({ status: 'running', db_stats: {} })
    }))
    window.history.pushState({}, '', '/macro-score')
    // When
    render(<App />)
    // Then
    expect(await screen.findByTestId('macro-score-value')).toHaveTextContent('73,125')
    expect(screen.getByRole('link', { name: 'Macro Score Beta' })).toHaveAttribute('aria-current', 'page')
    window.history.replaceState({}, '', '/')
  })
})
