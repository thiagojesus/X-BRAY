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
    await waitFor(() => expect(screen.getByText(/Sem dados disponíveis/)).toBeInTheDocument())
  })

  it('shows sem dados when data has no valid points', async () => {
    mockFetch.mockResolvedValue({ ok: true, json: () => Promise.resolve({ data: [] }) })
    renderPage()
    await waitFor(() => expect(screen.getByText(/Sem dados disponíveis/)).toBeInTheDocument())
  })

  it('renders KPIs and chart with valid IMA-B data', async () => {
    const apiData = {
      data: [
        { data: '01/05/2025', indice: 4900.1234, variacao_diaria: 0.5, variacao_12m: 10.2, duration: 4.5, pmr: 3 },
        { data: '01/06/2025', indice: 4950.5678, variacao_diaria: 0.6, variacao_12m: 11.1, duration: 4.6, pmr: 3 },
        { data: '01/07/2025', indice: 5000.9012, variacao_diaria: 0.7, variacao_12m: 12.0, duration: 4.7, pmr: 3 },
      ]
    }
    mockFetch.mockResolvedValue({ ok: true, json: () => Promise.resolve(apiData) })
    renderPage()
    await waitFor(() => {
      expect(screen.getByText('IMA-B (Índice)')).toBeInTheDocument()
    })
    expect(screen.getByText('Retorno 12 meses')).toBeInTheDocument()
    expect(screen.getByText('Duration')).toBeInTheDocument()
    expect(screen.getByText('5.000,9012')).toBeInTheDocument()
    expect(screen.getByText('12.00%')).toBeInTheDocument()
    expect(screen.getByText('IMA-B — Evolução do Índice')).toBeInTheDocument()
  })

  it('switches to Retorno 12m view', async () => {
    const apiData = {
      data: [
        { data: '01/05/2025', indice: 4900, variacao_diaria: 0.5, variacao_12m: 10.2, duration: 4.5, pmr: 3 },
        { data: '01/07/2025', indice: 5000, variacao_diaria: 0.7, variacao_12m: 12.0, duration: 4.7, pmr: 3 },
      ]
    }
    mockFetch.mockResolvedValue({ ok: true, json: () => Promise.resolve(apiData) })
    renderPage()
    await waitFor(() => expect(screen.getByText('IMA-B (Índice)')).toBeInTheDocument())
    const radio = screen.getByLabelText('Retorno 12m')
    radio.click()
    expect(screen.getByText('IMA-B — Retorno acumulado 12 meses')).toBeInTheDocument()
  })
})