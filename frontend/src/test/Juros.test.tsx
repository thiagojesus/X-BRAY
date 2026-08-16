import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest'
import { render, screen, waitFor } from '@testing-library/react'
import { MemoryRouter } from 'react-router-dom'
import Juros from '../pages/Juros'

const mockFetch = vi.fn()

beforeEach(() => {
  vi.stubGlobal('fetch', mockFetch)
})

afterEach(() => vi.restoreAllMocks())

function renderPage() {
  return render(
    <MemoryRouter>
      <Juros />
    </MemoryRouter>
  )
}

describe('Juros', () => {
  it('shows loading state', () => {
    mockFetch.mockReturnValue(new Promise(() => {}))
    renderPage()
    expect(screen.getByText('Carregando dados...')).toBeInTheDocument()
  })

  it('shows error state', async () => {
    mockFetch.mockRejectedValue(new Error('fail'))
    renderPage()
    await waitFor(() => expect(screen.getByText(/Erro ao carregar dados/)).toBeInTheDocument())
  })

  it('shows no-data state', async () => {
    mockFetch.mockResolvedValue({ ok: true, json: () => Promise.resolve({ data: null }) })
    renderPage()
    await waitFor(() => expect(screen.getByText('Erro ao carregar dados: Sem dados')).toBeInTheDocument())
  })

  it('renders KPI cards and charts with multi-date data', async () => {
    const apiData = {
      data: {
        selic_meta: [
          { data: '01/05/2025', valor: '14,50' },
          { data: '01/06/2025', valor: '14,65' },
          { data: '01/07/2025', valor: '14,75' },
        ],
        selic_efetiva: [
          { data: '01/06/2025', valor: '14,80' },
          { data: '01/07/2025', valor: '14,90' },
        ],
        cdi: [
          { data: '01/06/2025', valor: '14,80' },
          { data: '01/07/2025', valor: '14,90' },
        ],
        tr: [
          { data: '01/06/2025', valor: '0,75' },
          { data: '01/07/2025', valor: '0,80' },
        ],
        unknown_rate: [
          { data: '01/07/2025', valor: '10,00' },
        ],
      }
    }
    mockFetch.mockResolvedValue({ ok: true, json: () => Promise.resolve(apiData) })
    renderPage()
    await waitFor(() => {
      expect(screen.getByText('Selic Meta')).toBeInTheDocument()
      expect(screen.getByText('Selic Efetiva')).toBeInTheDocument()
      expect(screen.getByText('CDI')).toBeInTheDocument()
      expect(screen.getByText('TR')).toBeInTheDocument()
    })
    expect(screen.getByText('Taxa Meta Selic')).toBeInTheDocument()
    expect(screen.getByText('Taxas de Mercado (anualizadas)')).toBeInTheDocument()
  })

  it('renders with empty data arrays', async () => {
    const apiData = {
      data: {
        selic_meta: [],
        selic_efetiva: [],
        cdi: [],
        tr: [],
      }
    }
    mockFetch.mockResolvedValue({ ok: true, json: () => Promise.resolve(apiData) })
    renderPage()
    await waitFor(() => {
      expect(screen.getByText('Selic Meta')).toBeInTheDocument()
    })
  })
})
