import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest'
import { render, screen, waitFor, fireEvent } from '@testing-library/react'
import { MemoryRouter } from 'react-router-dom'
import Inflacao from '../pages/Inflacao'

const mockFetch = vi.fn()

beforeEach(() => {
  vi.stubGlobal('fetch', mockFetch)
})

afterEach(() => vi.restoreAllMocks())

function renderPage() {
  return render(
    <MemoryRouter>
      <Inflacao />
    </MemoryRouter>
  )
}

describe('Inflacao', () => {
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

  it('renders with multi-date data and exercises sort branches', async () => {
    const apiData = {
      data: {
        ipca: [
          { data: '01/05/2025', valor: '0,35' },
          { data: '01/06/2025', valor: '0,40' },
          { data: '01/07/2025', valor: '0,42' },
        ],
        ipca_12m: [
          { data: '01/05/2025', valor: '4,60' },
          { data: '01/07/2025', valor: '4,50' },
        ],
        ipca_15: [
          { data: '01/06/2025', valor: '0,39' },
          { data: '01/07/2025', valor: '0,38' },
        ],
        inpc: [{ data: '01/07/2025', valor: '0,45' }],
        igpm: [{ data: '01/07/2025', valor: '0,50' }],
        igpdi: [{ data: '01/07/2025', valor: '0,55' }],
        incc_di: [],
      }
    }
    mockFetch.mockResolvedValue({ ok: true, json: () => Promise.resolve(apiData) })
    renderPage()
    await waitFor(() => {
      expect(screen.getByText('Todos')).toBeInTheDocument()
    })
  })

  it('toggles individual series and exercises selectAll/selectNone', async () => {
    const apiData = {
      data: {
        ipca: [
          { data: '01/05/2025', valor: '0,35' },
          { data: '01/07/2025', valor: '0,42' },
        ],
        ipca_12m: [
          { data: '01/05/2025', valor: '4,60' },
          { data: '01/07/2025', valor: '4,50' },
        ],
        ipca_15: [{ data: '01/07/2025', valor: '0,38' }],
        inpc: [{ data: '01/07/2025', valor: '0,45' }],
        igpm: [{ data: '01/07/2025', valor: '0,50' }],
        igpdi: [{ data: '01/07/2025', valor: '0,55' }],
        incc_di: [{ data: '01/07/2025', valor: '0,60' }],
      }
    }
    mockFetch.mockResolvedValue({ ok: true, json: () => Promise.resolve(apiData) })
    renderPage()
    await waitFor(() => expect(screen.getByText('Todos')).toBeInTheDocument())

    const checkboxes = screen.getAllByRole('checkbox')
    expect(checkboxes.length).toBeGreaterThanOrEqual(1)

    fireEvent.click(screen.getByText('Nenhum'))
    fireEvent.click(screen.getByText('Todos'))

    fireEvent.click(checkboxes[0])
    fireEvent.click(checkboxes[0])

    fireEvent.click(checkboxes[1])
  })
})
