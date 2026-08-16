import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest'
import { render, screen, waitFor } from '@testing-library/react'
import { MemoryRouter } from 'react-router-dom'
import Focus from '../pages/Focus'

const mockFetch = vi.fn()

beforeEach(() => {
  vi.stubGlobal('fetch', mockFetch)
})

afterEach(() => vi.restoreAllMocks())

function renderPage() {
  return render(
    <MemoryRouter>
      <Focus />
    </MemoryRouter>
  )
}

describe('Focus', () => {
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

  it('shows no-data state when null', async () => {
    mockFetch.mockResolvedValue({ ok: true, json: () => Promise.resolve({ data: null }) })
    renderPage()
    await waitFor(() => expect(screen.getByText('Erro ao carregar dados: Sem dados')).toBeInTheDocument())
  })

  it('filters out empty indicators', async () => {
    const apiData = {
      data: {
        IPCA: [],
        Selic: [],
      }
    }
    mockFetch.mockResolvedValue({ ok: true, json: () => Promise.resolve(apiData) })
    renderPage()
    await waitFor(() => expect(screen.getByText(/Fonte:/)).toBeInTheDocument())
  })

  it('renders multiple indicator types with varied color branches', async () => {
    const apiData = {
      data: {
        IPCA: [
          { Data: '2025-01-01', Mediana: 4.5, Media: 4.6 },
          { Data: '2025-03-01', Mediana: 4.3, Media: 4.4 },
          { Data: '2025-06-01', Mediana: 4.2, Media: 4.3 },
        ],
        Selic: [
          { Data: '2025-01-01', Mediana: 12.0 },
          { Data: '2025-06-01', Mediana: 11.5 },
        ],
        PIB: [
          { Data: '2025-01-01', Mediana: 2.3 },
          { Data: '2025-06-01', Mediana: 2.1 },
        ],
        'Câmbio': [
          { Data: '2025-01-01', Mediana: 5.8, Media: 5.9 },
          { Data: '2025-06-01', Mediana: 5.5, Media: 5.6 },
        ],
        Geral: [
          { Data: '2025-01-01', Mediana: 3.0 },
          { Data: '2025-06-01', Mediana: 2.8 },
        ],
        Meta: [
          { Data: '2025-01-01', Mediana: 3.5 },
        ],
      }
    }
    mockFetch.mockResolvedValue({ ok: true, json: () => Promise.resolve(apiData) })
    renderPage()
    await waitFor(() => {
      expect(screen.getByText('IPCA')).toBeInTheDocument()
      expect(screen.getByText('Selic')).toBeInTheDocument()
      expect(screen.getByText('PIB')).toBeInTheDocument()
      expect(screen.getByText('Câmbio')).toBeInTheDocument()
      expect(screen.getByText('Geral')).toBeInTheDocument()
      expect(screen.getByText('Meta')).toBeInTheDocument()
    })
  })

  it('handles missing Mediana fallback to Media', async () => {
    const apiData = {
      data: {
        TestInd: [
          { Data: '2025-01-01', Mediana: null, Media: 3.0 },
          { Data: '2025-06-01', Mediana: null, Media: 2.8 },
        ],
      }
    }
    mockFetch.mockResolvedValue({ ok: true, json: () => Promise.resolve(apiData) })
    renderPage()
    await waitFor(() => expect(screen.getByText('TestInd')).toBeInTheDocument())
  })
})
