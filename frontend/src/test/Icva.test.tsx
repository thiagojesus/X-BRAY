import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest'
import { render, screen, waitFor } from '@testing-library/react'
import { MemoryRouter } from 'react-router-dom'
import Icva from '../pages/Icva'

const mockFetch = vi.fn()

beforeEach(() => {
  vi.stubGlobal('fetch', mockFetch)
})

afterEach(() => vi.restoreAllMocks())

function renderPage() {
  return render(
    <MemoryRouter>
      <Icva />
    </MemoryRouter>
  )
}

describe('Icva', () => {
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

  it('renders ICVA data with multiple months', async () => {
    const apiData = {
      data: {
        data: [
          { year: 2025, month: 1, nominal: 5.2, real: 3.1 },
          { year: 2025, month: 2, nominal: 4.8, real: 2.9 },
          { year: 2025, month: 3, nominal: 5.0, real: 3.0 },
        ],
        sectors: ['Varejo', 'Alimentos', 'Eletro', 'Tecnologia'],
        macro_sectors: {
          'Varejo Geral': ['Varejo', 'Alimentos'],
          'Duráveis': ['Eletro', 'Tecnologia'],
          'Serviços': [],
        }
      }
    }
    mockFetch.mockResolvedValue({ ok: true, json: () => Promise.resolve(apiData) })
    renderPage()
    await waitFor(() => {
      expect(screen.getByText('ICVA Nominal')).toBeInTheDocument()
      expect(screen.getByText('ICVA Real')).toBeInTheDocument()
      expect(screen.getByText('Varejo Geral')).toBeInTheDocument()
    })
    const varejoElements = screen.getAllByText('Varejo')
    expect(varejoElements.length).toBeGreaterThanOrEqual(1)
  })

  it('renders with empty data array', async () => {
    const apiData = {
      data: {
        data: [],
        sectors: [],
        macro_sectors: {},
      }
    }
    mockFetch.mockResolvedValue({ ok: true, json: () => Promise.resolve(apiData) })
    renderPage()
    await waitFor(() => {
      expect(screen.getByText('ICVA Nominal')).toBeInTheDocument()
    })
  })

  it('handles data.data without nested data field', async () => {
    const apiData = {
      data: [
        { year: 2025, month: 1, nominal: 5.2, real: 3.1 },
      ]
    }
    mockFetch.mockResolvedValue({ ok: true, json: () => Promise.resolve(apiData) })
    renderPage()
    await waitFor(() => {
      expect(screen.getByText('ICVA Nominal')).toBeInTheDocument()
    })
  })
})
