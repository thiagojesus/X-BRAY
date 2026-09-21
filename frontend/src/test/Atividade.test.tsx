import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest'
import { render, screen, waitFor } from '@testing-library/react'
import { MemoryRouter } from 'react-router-dom'
import Atividade from '../pages/Atividade'

const mockFetch = vi.fn()

beforeEach(() => {
  vi.stubGlobal('fetch', mockFetch)
})

afterEach(() => vi.restoreAllMocks())

function renderPage() {
  return render(
    <MemoryRouter>
      <Atividade />
    </MemoryRouter>
  )
}

describe('Atividade', () => {
  it('characterizes the activity endpoint and percentage series', async () => {
    // Given
    mockFetch.mockResolvedValue(Response.json({
      data: {
        pib: [{ data: '01/01/2025', valor: '2200000000000' }],
        ibc_br: [{ data: '01/07/2025', valor: '105,2' }],
        desemprego: [{ data: '01/07/2025', valor: '6,5' }],
        resultado_primario: [{ data: '01/07/2025', valor: '45000' }],
      },
    }))

    // When
    renderPage()

    // Then
    expect(await screen.findByText('6.5%')).toBeInTheDocument()
    expect(screen.getByText('R$ 2.20 tri')).toBeInTheDocument()
    expect(screen.getByText('105,2')).toBeInTheDocument()
    expect(screen.queryByText('R$ 45.000 mi')).not.toBeInTheDocument()
    expect(screen.queryByText(/Resultado Primário/)).not.toBeInTheDocument()
    expect(screen.getByText('IBC-Br (índice)')).toBeInTheDocument()
    expect(mockFetch).toHaveBeenCalledWith('/api/atividade', expect.objectContaining({ signal: expect.any(AbortSignal) }))
  })

  it('[RED XBR-001] renders corrected indicators by formatting IBC-Br as an index', async () => {
    // Given
    mockFetch.mockResolvedValue(Response.json({
      data: {
        pib: [],
        ibc_br: [{ data: '01/07/2025', valor: '153.42' }],
        desemprego: [],
        resultado_primario: [],
      },
    }))

    // When
    renderPage()

    // Then
    expect(await screen.findByText('153,4')).toBeInTheDocument()
    expect(screen.queryByText('153,4%')).not.toBeInTheDocument()
  })

  it('handles regression edge cases when the latest IBC-Br value is null', async () => {
    // Given
    mockFetch.mockResolvedValue(Response.json({
      data: {
        pib: [],
        ibc_br: [{ data: '01/07/2025', valor: null }],
        desemprego: [],
        resultado_primario: [],
      },
    }))

    // When
    renderPage()

    // Then
    const ibcCard = (await screen.findByText('IBC-Br (índice)')).closest('.kpi-card')
    expect(ibcCard).toHaveTextContent('—')
    expect(ibcCard).not.toHaveTextContent('NaN')
  })

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

  it('renders all KPI cards and charts with data', async () => {
    const apiData = {
      data: {
        pib: [
          { data: '01/01/2024', valor: '2170000000000' },
          { data: '01/01/2025', valor: '2200000000000' },
        ],
        ibc_br: [
          { data: '01/06/2025', valor: '105,1' },
          { data: '01/07/2025', valor: '105,2' },
        ],
        desemprego: [
          { data: '01/06/2025', valor: '6,6' },
          { data: '01/07/2025', valor: '6,5' },
        ],
        resultado_primario: [
          { data: '01/06/2025', valor: '44000' },
          { data: '01/07/2025', valor: '45000' },
        ],
      }
    }
    mockFetch.mockResolvedValue({ ok: true, json: () => Promise.resolve(apiData) })
    renderPage()
    await waitFor(() => {
      expect(screen.getByText('PIB (R$)')).toBeInTheDocument()
      expect(screen.getByText('IBC-Br (índice)')).toBeInTheDocument()
      expect(screen.getByText('Desemprego (%)')).toBeInTheDocument()
      expect(screen.queryByText(/Resultado Primário/)).not.toBeInTheDocument()
    })
  })

  it('renders with medium PIB value (bi range)', async () => {
    const apiData = {
      data: {
        pib: [{ data: '01/01/2024', valor: '500000000000' }],
        ibc_br: [{ data: '01/07/2025', valor: '105,2' }],
        desemprego: [],
        resultado_primario: [{ data: '01/07/2025', valor: '5000' }],
      }
    }
    mockFetch.mockResolvedValue({ ok: true, json: () => Promise.resolve(apiData) })
    renderPage()
    await waitFor(() => {
      expect(screen.getByText('PIB (R$)')).toBeInTheDocument()
    })
  })

  it('renders with small PIB value (local range)', async () => {
    const apiData = {
      data: {
        pib: [{ data: '01/01/2024', valor: '500000000' }],
        ibc_br: [],
        desemprego: [],
        resultado_primario: [],
      }
    }
    mockFetch.mockResolvedValue({ ok: true, json: () => Promise.resolve(apiData) })
    renderPage()
    await waitFor(() => {
      expect(screen.getByText('PIB (R$)')).toBeInTheDocument()
    })
  })
})
