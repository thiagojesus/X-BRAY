import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest'
import { render, screen, waitFor } from '@testing-library/react'
import { MemoryRouter } from 'react-router-dom'
import Cambio from '../pages/Cambio'

const mockFetch = vi.fn()

beforeEach(() => {
  vi.stubGlobal('fetch', mockFetch)
})

afterEach(() => vi.restoreAllMocks())

function renderPage() {
  return render(
    <MemoryRouter>
      <Cambio />
    </MemoryRouter>
  )
}

describe('Cambio', () => {
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
        ptax_compra_usd: [
          { data: '01/05/2025', valor: '5,38' },
          { data: '01/06/2025', valor: '5,40' },
          { data: '01/07/2025', valor: '5,45' },
        ],
        ptax_venda_usd: [
          { data: '01/05/2025', valor: '5,46' },
          { data: '01/06/2025', valor: '5,48' },
          { data: '01/07/2025', valor: '5,50' },
        ],
        eur_brl: [
          { data: '01/06/2025', valor: '6,05' },
          { data: '01/07/2025', valor: '6,10' },
        ],
      }
    }
    mockFetch.mockResolvedValue({ ok: true, json: () => Promise.resolve(apiData) })
    renderPage()
    await waitFor(() => {
      expect(screen.getByText('PTAX Compra USD')).toBeInTheDocument()
      expect(screen.getByText('PTAX Venda USD')).toBeInTheDocument()
      expect(screen.getByText('EUR/BRL')).toBeInTheDocument()
    })
  })

  it('renders with no KPI data', async () => {
    const apiData = {
      data: {
        ptax_compra_usd: [],
        ptax_venda_usd: [],
        eur_brl: [],
      }
    }
    mockFetch.mockResolvedValue({ ok: true, json: () => Promise.resolve(apiData) })
    renderPage()
    await waitFor(() => {
      expect(screen.getByText('PTAX Compra USD')).toBeInTheDocument()
    })
  })
})
