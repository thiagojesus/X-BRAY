import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest'
import { render, screen, waitFor } from '@testing-library/react'
import { MemoryRouter } from 'react-router-dom'
import Dashboard from '../pages/Dashboard'

const mockFetch = vi.fn()

beforeEach(() => {
  vi.stubGlobal('fetch', mockFetch)
})

afterEach(() => vi.restoreAllMocks())

function renderDashboard() {
  return render(
    <MemoryRouter>
      <Dashboard />
    </MemoryRouter>
  )
}

describe('Dashboard', () => {
  it('characterizes the status endpoint and dashboard navigation', async () => {
    // Given
    mockFetch.mockResolvedValue(Response.json({
      status: 'running',
      db_stats: { sgs: 10 },
    }))

    // When
    renderDashboard()

    // Then
    expect(await screen.findByText(/Online/)).toBeInTheDocument()
    expect(screen.getByRole('link', { name: /Complementares/i })).toHaveAttribute('href', '/complementares')
    expect(mockFetch).toHaveBeenCalledWith('/api/status', expect.objectContaining({ signal: expect.any(AbortSignal) }))
  })

  it('[RED XBR-001] renders corrected indicators by totaling finite database records', async () => {
    // Given
    mockFetch.mockResolvedValue(Response.json({
      status: 'running',
      db_stats: { sgs: 10, focus: 2, meta: 1 },
    }))

    // When
    renderDashboard()

    // Then
    expect(await screen.findByText('Caches: 13 registros armazenados')).toBeInTheDocument()
    expect(screen.queryByText(/séries armazenadas/)).not.toBeInTheDocument()
  })

  it('handles regression edge cases by ignoring non-numeric database stats', async () => {
    // Given
    mockFetch.mockResolvedValue(Response.json({
      status: 'running',
      db_stats: { sgs: 10, focus: 2, meta: 1, label: 'nove', missing: null },
    }))

    // When
    renderDashboard()

    // Then
    expect(await screen.findByText('Caches: 13 registros armazenados')).toBeInTheDocument()
    expect(screen.queryByText(/NaN/)).not.toBeInTheDocument()
  })

  it('handles regression edge cases when database stats are absent', async () => {
    // Given
    mockFetch.mockResolvedValue(Response.json({ status: 'running' }))

    // When
    renderDashboard()

    // Then
    expect(await screen.findByText('Caches: 0 registros armazenados')).toBeInTheDocument()
  })

  it('shows loading state', () => {
    mockFetch.mockReturnValue(new Promise(() => {}))
    renderDashboard()
    expect(screen.getByText('Carregando dados...')).toBeInTheDocument()
  })

  it('shows error state', async () => {
    mockFetch.mockRejectedValue(new Error('fail'))
    renderDashboard()
    await waitFor(() => expect(screen.getByText(/Erro ao carregar dados/)).toBeInTheDocument())
  })

  it('shows hero section', async () => {
    mockFetch.mockResolvedValue({ ok: true, json: () => Promise.resolve({ status: 'running', cache: {} }) })
    renderDashboard()
    expect(screen.getByAltText('X-BRAY')).toBeInTheDocument()
    expect(screen.getByText('Raio-X do Macro Brasileiro')).toBeInTheDocument()
  })

  it('shows status bar when data loads', async () => {
    mockFetch.mockResolvedValue({ ok: true, json: () => Promise.resolve({ status: 'running', cache: { s1: [], s2: [] } }) })
    renderDashboard()
    await waitFor(() => expect(screen.getByText(/Online/)).toBeInTheDocument())
  })

  it('shows offline status', async () => {
    mockFetch.mockResolvedValue({ ok: true, json: () => Promise.resolve({ status: 'stopped', cache: {} }) })
    renderDashboard()
    await waitFor(() => expect(screen.getByText(/Offline/)).toBeInTheDocument())
  })

  it('renders all dashboard cards', async () => {
    mockFetch.mockResolvedValue({ ok: true, json: () => Promise.resolve({ status: 'running', cache: {} }) })
    renderDashboard()
    await waitFor(() => {
      expect(screen.getByText('Taxas de Juros')).toBeInTheDocument()
      expect(screen.getByText('Inflação')).toBeInTheDocument()
      expect(screen.getByText('IPCA Decomposição')).toBeInTheDocument()
      expect(screen.getByText('Atividade Econômica')).toBeInTheDocument()
      expect(screen.getByText('Câmbio')).toBeInTheDocument()
      expect(screen.getByText('Títulos Públicos')).toBeInTheDocument()
      expect(screen.getByText('Tesouro Direto')).toBeInTheDocument()
      expect(screen.getByText('Expectativas FOCUS')).toBeInTheDocument()
      expect(screen.getByText('Complementares')).toBeInTheDocument()
      expect(screen.getByText('Reservas, IC-Br, M0/M1/M2')).toBeInTheDocument()
      expect(screen.queryByText('Reservas, Base Monetária')).not.toBeInTheDocument()
    })
  })

  it('links to the Elections dashboard', async () => {
    mockFetch.mockResolvedValue({ ok: true, json: () => Promise.resolve({ status: 'running', cache: {} }) })
    renderDashboard()

    const electionsLink = await screen.findByRole('link', { name: /Eleições/i })
    expect(electionsLink).toHaveAttribute('href', '/eleicoes')
  })

  it('[RED XBR-102] links to the Fiscal dashboard', async () => {
    mockFetch.mockResolvedValue(Response.json({ status: 'running', db_stats: {} }))
    renderDashboard()

    const fiscalLink = await screen.findByRole('link', { name: /Fiscal/i })
    expect(fiscalLink).toHaveAttribute('href', '/fiscal')
    expect(fiscalLink).toHaveTextContent('DBGG, DLSP e resultados fiscais')
  })

  it('[RED XBR-202] links to the Crédito e Famílias dashboard', async () => {
    mockFetch.mockResolvedValue(Response.json({ status: 'running', db_stats: {} }))
    renderDashboard()

    const creditLink = await screen.findByRole('link', { name: /Crédito e Famílias/i })
    expect(creditLink).toHaveAttribute('href', '/credito')
    expect(creditLink).toHaveTextContent('Juros, orçamento e inadimplência')
  })
})
