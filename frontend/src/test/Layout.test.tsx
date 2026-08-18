import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest'
import { render, screen, within, fireEvent, waitFor } from '@testing-library/react'
import { MemoryRouter } from 'react-router-dom'
import Layout from '../components/Layout'

const mockFetch = vi.fn()

beforeEach(() => {
  vi.stubGlobal('fetch', mockFetch)
  mockFetch.mockResolvedValue({ ok: true })
})

afterEach(() => vi.restoreAllMocks())

function renderLayout(route = '/') {
  return render(
    <MemoryRouter initialEntries={[route]}>
      <Layout />
    </MemoryRouter>
  )
}

describe('Layout', () => {
  it('renders sidebar header', () => {
    renderLayout()
    expect(screen.getByAltText('X-BRAY')).toBeInTheDocument()
    expect(screen.getByText('Macro Brasil')).toBeInTheDocument()
  })

  it('renders all navigation links', () => {
    renderLayout()
    const nav = screen.getByRole('navigation')
    expect(within(nav).getByText('Dashboard')).toBeInTheDocument()
    expect(within(nav).getByText('Taxas de Juros')).toBeInTheDocument()
    expect(within(nav).getByText('Inflação')).toBeInTheDocument()
    expect(within(nav).getByText('IPCA Decomposição')).toBeInTheDocument()
    expect(within(nav).getByText('Atividade Econômica')).toBeInTheDocument()
    expect(within(nav).getByText('Câmbio')).toBeInTheDocument()
    expect(within(nav).getByText('IMA-B')).toBeInTheDocument()
    expect(within(nav).getByText('Tesouro Direto')).toBeInTheDocument()
    expect(within(nav).getByText('Expectativas FOCUS')).toBeInTheDocument()
    expect(within(nav).getByText('Complementares')).toBeInTheDocument()
  })

  it('renders refresh button', () => {
    renderLayout()
    expect(screen.getByText('Refresh Agora')).toBeInTheDocument()
  })

  it('renders page header with default Dashboard label', () => {
    renderLayout()
    const header = screen.getByRole('banner')
    expect(within(header).getByText('Dashboard')).toBeInTheDocument()
  })

  it('renders page header for different routes', () => {
    renderLayout('/juros')
    const header = screen.getByRole('banner')
    expect(within(header).getByText('Taxas de Juros')).toBeInTheDocument()
  })

  it('calls fetch on refresh button click', async () => {
    mockFetch.mockResolvedValue({ ok: true })
    renderLayout()
    fireEvent.click(screen.getByText('Refresh Agora'))
    await waitFor(() => {
      expect(mockFetch).toHaveBeenCalledWith('/api/refresh', { method: 'POST' })
    })
  })

  it('shows Atualizando... while refreshing', async () => {
    let resolveFetch: () => void
    mockFetch.mockImplementation(() => new Promise<void>(r => { resolveFetch = r }))
    renderLayout()
    fireEvent.click(screen.getByText('Refresh Agora'))
    expect(screen.getByText('Atualizando...')).toBeInTheDocument()
    resolveFetch!()
    await waitFor(() => {
      expect(screen.getByText('Refresh Agora')).toBeInTheDocument()
    })
  })

  it('renders last update info', () => {
    renderLayout()
    expect(screen.getByText(/refresh diário 06:00 BRT/)).toBeInTheDocument()
  })

  it('renders last update label from status endpoint', async () => {
    mockFetch.mockImplementation((url: string) => {
      if (url === '/api/status') {
        return Promise.resolve({ ok: true, json: () => Promise.resolve({ last_updated: '2026-08-17T21:46:49.583210' }) })
      }
      return Promise.resolve({ ok: true, json: () => Promise.resolve({}) })
    })
    renderLayout()
    await waitFor(() => {
      expect(screen.getByText(/Última atualização: 17\/08\/2026, 21:46/)).toBeInTheDocument()
    })
  })
})
