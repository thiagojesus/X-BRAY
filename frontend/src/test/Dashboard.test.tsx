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
    })
  })
})
