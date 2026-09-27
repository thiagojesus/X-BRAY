import { fireEvent, render, screen, waitFor } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { MemoryRouter } from 'react-router-dom'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'

import Atividade from '../pages/Atividade'

const mockFetch = vi.fn()

const legacyPayload = {
  source: 'BCB SGS',
  data: {
    pib: [{ data: '01/01/2025', valor: '2200000000000' }],
    ibc_br: [{ data: '01/07/2025', valor: '105.2' }],
    desemprego: [{ data: '01/07/2025', valor: '6.5' }],
    resultado_primario: [{ data: '01/07/2025', valor: '45000' }],
  },
} as const

const levelData = {
  ibc_br_sa: [{ data: '01/06/2025', valor: '100' }, { data: '01/07/2025', valor: '110' }],
  ibc_agro_sa: [{ data: '01/06/2025', valor: '80' }, { data: '01/07/2025', valor: '84' }],
  ibc_industria_sa: [{ data: '01/06/2025', valor: '90' }, { data: '01/07/2025', valor: '99' }],
  ibc_servicos_sa: [{ data: '01/06/2025', valor: '120' }, { data: '01/07/2025', valor: '126' }],
  ibc_ex_agro_sa: [{ data: '01/06/2025', valor: '105' }, { data: '01/07/2025', valor: '115.5' }],
  rendimento_medio_real: [{ data: '01/06/2025', valor: '3000' }, { data: '01/07/2025', valor: '3030' }],
} as const

const momData = Object.fromEntries(
  Object.keys(levelData).map(key => [key, [{ data: '01/07/2025', valor: 10 }]]),
)

const yoyData = Object.fromEntries(
  Object.keys(levelData).map(key => [key, [{ data: '01/07/2025', valor: 21 }]]),
)

function sectoralPayload(mode: 'level' | 'mom' | 'yoy') {
  return {
    source: 'BCB SGS',
    mode,
    status: 'ok',
    last_updated: '2025-07-01',
    unavailable: [],
    metadata: {},
    data: mode === 'level' ? levelData : mode === 'mom' ? momData : yoyData,
  }
}

function mockSuccessfulRequests(): void {
  mockFetch.mockImplementation((input: RequestInfo | URL) => {
    const url = String(input)
    if (url === '/api/atividade') return Promise.resolve(Response.json(legacyPayload))
    if (url.includes('mode=level')) return Promise.resolve(Response.json(sectoralPayload('level')))
    if (url.includes('mode=mom')) return Promise.resolve(Response.json(sectoralPayload('mom')))
    if (url.includes('mode=yoy')) return Promise.resolve(Response.json(sectoralPayload('yoy')))
    return Promise.reject(new Error(`Unexpected request: ${url}`))
  })
}

function renderPage() {
  return render(
    <MemoryRouter>
      <Atividade />
    </MemoryRouter>,
  )
}

beforeEach(() => {
  vi.stubGlobal('fetch', mockFetch)
})

afterEach(() => vi.restoreAllMocks())

describe('Atividade', () => {
  it('renders four required KPIs and three charts without presenting legacy fiscal data', async () => {
    mockSuccessfulRequests()

    renderPage()

    expect(await screen.findByText('IBC-Br m/m')).toBeInTheDocument()
    expect(screen.getByText('10,0%')).toBeInTheDocument()
    expect(screen.getByText('IBC-Br 12m')).toBeInTheDocument()
    expect(screen.getByText('21,0%')).toBeInTheDocument()
    expect(screen.getByText('Desemprego')).toBeInTheDocument()
    expect(screen.getByText('6,5%')).toBeInTheDocument()
    expect(screen.getByText('Rendimento médio real')).toBeInTheDocument()
    expect(screen.getByText('R$ 3.030,00')).toBeInTheDocument()
    expect(screen.getByText('IBC-Br total × ex-agro')).toBeInTheDocument()
    expect(screen.getByText('Agro × indústria × serviços')).toBeInTheDocument()
    expect(screen.getByText('Desemprego × rendimento real')).toBeInTheDocument()
    expect(screen.queryByText('PIB (R$)')).not.toBeInTheDocument()
    expect(screen.queryByText(/Resultado primário/i)).not.toBeInTheDocument()
  })

  it('switches chart modes with an accessible pressed state and never labels level indexes as percentages', async () => {
    const user = userEvent.setup()
    mockSuccessfulRequests()

    renderPage()

    const level = await screen.findByRole('button', { name: 'Nível' })
    const mom = screen.getByRole('button', { name: 'Variação mensal' })
    const yoy = screen.getByRole('button', { name: 'Variação 12m' })
    expect(level).toHaveAttribute('aria-pressed', 'true')
    expect(mom).toHaveAttribute('aria-pressed', 'false')
    expect(screen.getByText('Índice (base 100 na janela)')).toBeInTheDocument()
    expect(screen.queryByText('Índice (%)')).not.toBeInTheDocument()

    await user.click(mom)
    expect(mom).toHaveAttribute('aria-pressed', 'true')
    expect(level).toHaveAttribute('aria-pressed', 'false')
    expect(screen.getAllByText('Variação mensal (%)').length).toBeGreaterThan(0)

    await user.click(yoy)
    expect(yoy).toHaveAttribute('aria-pressed', 'true')
    expect(screen.getAllByText('Variação 12m (%)').length).toBeGreaterThan(0)
  })

  it('keeps available charts and reports transformation failures without NaN or Infinity', async () => {
    mockSuccessfulRequests()
    mockFetch.mockImplementation((input: RequestInfo | URL) => {
      const url = String(input)
      if (url === '/api/atividade') return Promise.resolve(Response.json(legacyPayload))
      if (url.includes('mode=level')) return Promise.resolve(Response.json(sectoralPayload('level')))
      if (url.includes('mode=yoy')) return Promise.resolve(Response.json(sectoralPayload('yoy')))
      if (url.includes('mode=mom')) {
        return Promise.resolve(Response.json({
          ...sectoralPayload('mom'),
          status: 'partial',
          unavailable: ['ibc_agro_sa'],
          data: { ...momData, ibc_agro_sa: [] },
        }))
      }
      return Promise.reject(new Error(`Unexpected request: ${url}`))
    })

    renderPage()
    fireEvent.click(await screen.findByRole('button', { name: 'Variação mensal' }))

    expect(await screen.findByRole('status')).toHaveTextContent('Fonte parcialmente indisponível.')
    expect(screen.getAllByText(/Agropecuária/).length).toBeGreaterThan(0)
    expect(document.body).not.toHaveTextContent('NaN')
    expect(document.body).not.toHaveTextContent('Infinity')
    expect(document.querySelectorAll('.chart-container')).toHaveLength(3)
  })

  it('shows a loading state while required requests are pending', () => {
    mockFetch.mockReturnValue(new Promise(() => {}))

    renderPage()

    expect(screen.getByText('Carregando dados...')).toBeInTheDocument()
  })

  it('shows an error state when required data cannot be loaded', async () => {
    mockFetch.mockRejectedValue(new Error('fail'))

    renderPage()

    await waitFor(() => expect(screen.getByRole('alert')).toHaveTextContent('Erro ao carregar dados: fail'))
  })
})
