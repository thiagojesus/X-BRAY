import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest'
import { render, screen, waitFor } from '@testing-library/react'
import { MemoryRouter } from 'react-router-dom'
import Titulos from '../pages/Titulos'

const mockFetch = vi.fn()

beforeEach(() => {
  vi.stubGlobal('fetch', mockFetch)
})

afterEach(() => vi.restoreAllMocks())

function renderPage() {
  return render(
    <MemoryRouter>
      <Titulos />
    </MemoryRouter>
  )
}

describe('Titulos', () => {
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

  it('shows sem dados when data.data is null', async () => {
    mockFetch.mockResolvedValue({ ok: true, json: () => Promise.resolve({ data: null }) })
    renderPage()
    await waitFor(() => expect(screen.getByText('Erro ao carregar dados: Sem dados')).toBeInTheDocument())
  })

  it('shows empty sheets message', async () => {
    mockFetch.mockResolvedValue({ ok: true, json: () => Promise.resolve({ data: {} }) })
    renderPage()
    await waitFor(() => expect(screen.getByText(/Nenhuma aba encontrada/)).toBeInTheDocument())
  })

  it('renders tables with multi-row data', async () => {
    const apiData = {
      data: {
        IMA_B: [
          { Data: '01/05/2025', Preco: 4900, Pu: 99, NullCol: null },
          { Data: '01/06/2025', Preco: 4950, Pu: 100 },
          { Data: '01/07/2025', Preco: 5000, Pu: 101 },
        ]
      }
    }
    mockFetch.mockResolvedValue({ ok: true, json: () => Promise.resolve(apiData) })
    renderPage()
    await waitFor(() => {
      expect(screen.getByText('IMA B')).toBeInTheDocument()
    })
  })

  it('truncates long tables at 50 rows', async () => {
    const rows = Array.from({ length: 60 }, (_, i) => ({ Data: `01/${String(i % 12 + 1).padStart(2, '0')}/2025`, Val: i }))
    mockFetch.mockResolvedValue({ ok: true, json: () => Promise.resolve({ data: { Sheet1: rows } }) })
    renderPage()
    await waitFor(() => expect(screen.getByText(/Mostrando 50 de 60/)).toBeInTheDocument())
  })

  it('skips non-array and empty sheets but renders valid ones', async () => {
    const apiData = {
      data: {
        BadSheet: 'not an array',
        EmptySheet: [],
        Valid_Sheet: [{ Data: '01/07/2025', Val: 1 }],
      }
    }
    mockFetch.mockResolvedValue({ ok: true, json: () => Promise.resolve(apiData) })
    renderPage()
    await waitFor(() => expect(screen.getByText('VALID SHEET')).toBeInTheDocument())
    expect(screen.queryByText('BAD SHEET')).not.toBeInTheDocument()
    expect(screen.queryByText('EMPTY SHEET')).not.toBeInTheDocument()
  })
})
