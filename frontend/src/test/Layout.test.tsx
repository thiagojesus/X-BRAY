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
})
