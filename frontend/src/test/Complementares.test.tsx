import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest'
import { render, screen, waitFor } from '@testing-library/react'
import { MemoryRouter } from 'react-router-dom'
import Complementares from '../pages/Complementares'

const mockFetch = vi.fn()

beforeEach(() => {
  vi.stubGlobal('fetch', mockFetch)
})

afterEach(() => vi.restoreAllMocks())

function renderPage() {
  return render(
    <MemoryRouter>
      <Complementares />
    </MemoryRouter>
  )
}

describe('Complementares', () => {
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

  it('shows no-data message when data is empty', async () => {
    mockFetch.mockResolvedValue({ ok: true, json: () => Promise.resolve({ data: {} }) })
    renderPage()
    await waitFor(() => {
      expect(screen.getByText(/Sem dados disponíveis/)).toBeInTheDocument()
    })
  })

  it('shows error when data.data is null', async () => {
    mockFetch.mockResolvedValue({ ok: true, json: () => Promise.resolve({ data: null }) })
    renderPage()
    await waitFor(() => {
      expect(screen.getByText(/Erro ao carregar dados: Sem dados/)).toBeInTheDocument()
    })
  })

  it('renders KPI cards and charts with multi-date data', async () => {
    const apiData = {
      data: {
        reservas_internacionais: [
          { data: '01/05/2025', valor: '345000' },
          { data: '01/06/2025', valor: '348000' },
          { data: '01/07/2025', valor: '350000' },
        ],
        m0: [
          { data: '01/06/2025', valor: '431100' },
          { data: '01/07/2025', valor: '432500' },
        ],
        m1: [
          { data: '01/06/2025', valor: '646296' },
          { data: '01/07/2025', valor: '647800' },
        ],
        m2: [
          { data: '01/06/2025', valor: '7646640' },
          { data: '01/07/2025', valor: '7650000' },
        ],
        ic_commodities: [
          { data: '01/07/2025', valor: '120,5' },
        ],
      }
    }
    mockFetch.mockResolvedValue({ ok: true, json: () => Promise.resolve(apiData) })
    renderPage()
    await waitFor(() => {
      expect(screen.getByText('Reservas (USD mi)')).toBeInTheDocument()
      expect(screen.getByText('M0 (R$ mi)')).toBeInTheDocument()
      expect(screen.getByText('M1 (R$ mi)')).toBeInTheDocument()
      expect(screen.getByText('M2 (R$ mi)')).toBeInTheDocument()
    })
    const icElements = screen.getAllByText('IC-Br Commodities')
    expect(icElements.length).toBeGreaterThanOrEqual(1)
    expect(screen.getByText('Reservas Internacionais')).toBeInTheDocument()
    expect(screen.getByText('Agregados Monetários (M0, M1, M2)')).toBeInTheDocument()
  })

  it('renders with no KPI values but has data entries', async () => {
    const apiData = {
      data: {
        reservas_internacionais: [{ data: '01/07/2025', valor: '0' }],
        base_monetaria: [{ data: '01/07/2025', valor: '0' }],
        ic_commodities: [{ data: '01/07/2025', valor: '0' }],
      }
    }
    mockFetch.mockResolvedValue({ ok: true, json: () => Promise.resolve(apiData) })
    renderPage()
    await waitFor(() => {
      expect(screen.getByText('Reservas (USD mi)')).toBeInTheDocument()
    })
  })
})
