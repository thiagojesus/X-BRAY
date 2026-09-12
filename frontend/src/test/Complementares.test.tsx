import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest'
import { fireEvent, render, screen, waitFor } from '@testing-library/react'
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
  it('characterizes visible monetary aggregates without duplicating legacy base monetary data', async () => {
    // Given
    mockFetch.mockResolvedValue(Response.json({
      data: {
        base_monetaria: [{ data: '01/07/2025', valor: '432500' }],
        m0: [{ data: '01/07/2025', valor: '432500' }],
        m1: [{ data: '01/07/2025', valor: '647800' }],
        m2: [{ data: '01/07/2025', valor: '7650000' }],
      },
    }))

    // When
    renderPage()

    // Then
    expect(await screen.findByText('M0 (R$ mi)')).toBeInTheDocument()
    expect(screen.getByText('M1 (R$ mi)')).toBeInTheDocument()
    expect(screen.getByText('M2 (R$ mi)')).toBeInTheDocument()
    expect(screen.queryByText('Base Monetária (R$ mi)')).not.toBeInTheDocument()
    expect(mockFetch).toHaveBeenCalledWith('/api/complementares', expect.objectContaining({ signal: expect.any(AbortSignal) }))
  })

  it('[RED XBR-001] renders corrected indicators with the approved terms-of-trade description', async () => {
    // Given
    mockFetch.mockResolvedValue(Response.json({
      data: {
        ic_commodities: [{ data: '01/07/2025', valor: '120,5' }],
      },
    }))
    renderPage()
    await screen.findByText('IC-Br Commodities', { selector: '.kpi-label' })
    const trigger = screen.getByRole('button', { name: 'Informações sobre IC-Br Commodities' })

    // When
    fireEvent.mouseEnter(trigger)

    // Then
    expect(screen.getByText('Índice de Commodities Brasil do BCB — acompanha os preços, em reais, de commodities relevantes para a economia brasileira e para a análise dos termos de troca.')).toBeInTheDocument()
    expect(screen.queryByText(/termos de troque|impacta câmbio/)).not.toBeInTheDocument()
  })

  it('[RED XBR-001] renders corrected indicators without an unsupported M0 causal claim', async () => {
    // Given
    mockFetch.mockResolvedValue(Response.json({
      data: {
        m0: [{ data: '01/07/2025', valor: '432500' }],
      },
    }))
    renderPage()
    await screen.findByText('M0 (R$ mi)', { selector: '.kpi-label' })
    const trigger = screen.getByRole('button', { name: 'Informações sobre M0 (R$ mi)' })

    // When
    fireEvent.mouseEnter(trigger)

    // Then
    expect(screen.getByText('M0 — base monetária: papel-moeda emitido mais reservas bancárias.')).toBeInTheDocument()
    expect(screen.queryByText(/reflete a política monetária/)).not.toBeInTheDocument()
  })

  it('handles regression edge cases when only legacy base monetary data is available', async () => {
    // Given
    mockFetch.mockResolvedValue(Response.json({
      data: {
        base_monetaria: [{ data: '01/07/2025', valor: '432500' }],
      },
    }))

    // When
    renderPage()

    // Then
    expect(await screen.findByText(/Sem dados disponíveis/)).toBeInTheDocument()
    expect(screen.queryByText('Base Monetária (R$ mi)')).not.toBeInTheDocument()
    expect(screen.queryByText('Agregados Monetários (M0, M1, M2)')).not.toBeInTheDocument()
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
      expect(screen.getByText('USD 350.000 mi')).toBeInTheDocument()
      expect(screen.getByText('R$ 432.500')).toBeInTheDocument()
      expect(screen.getByText('R$ 647.800')).toBeInTheDocument()
      expect(screen.getByText('R$ 7.650.000')).toBeInTheDocument()
      expect(screen.getByText('120.5')).toBeInTheDocument()
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
