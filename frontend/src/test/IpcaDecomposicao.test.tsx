import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest'
import { render, screen, waitFor, fireEvent } from '@testing-library/react'
import { MemoryRouter } from 'react-router-dom'
import IpcaDecomposicao from '../pages/IpcaDecomposicao'

const mockFetch = vi.fn()

beforeEach(() => {
  vi.stubGlobal('fetch', mockFetch)
})

afterEach(() => vi.restoreAllMocks())

function renderPage() {
  return render(
    <MemoryRouter>
      <IpcaDecomposicao />
    </MemoryRouter>
  )
}

describe('IpcaDecomposicao', () => {
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
    mockFetch.mockResolvedValue({ ok: true, json: () => Promise.resolve(null) })
    renderPage()
    await waitFor(() => expect(screen.getByText('Erro ao carregar dados: Sem dados')).toBeInTheDocument())
  })

  it('renders with multi-date data and exercises sort branches', async () => {
    const apiData = {
      grupos: {
        alimentacao_bebidas: [
          { data: '01/05/2025', valor: '0,45' },
          { data: '01/06/2025', valor: '0,48' },
          { data: '01/07/2025', valor: '0,50' },
        ],
        habitacao: [
          { data: '01/06/2025', valor: '0,28' },
          { data: '01/07/2025', valor: '0,30' },
        ],
        artigos_residencia: [],
        vestuario: [{ data: '01/07/2025', valor: '0,15' }],
        transportes: [],
        comunicacao: [],
        saude_cuidados: [],
        despesas_pessoais: [],
      },
      naturezas: {
        bens_duraveis: [{ data: '01/07/2025', valor: '0,12' }],
        bens_semi_duraveis: [],
        bens_nao_duraveis: [{ data: '01/07/2025', valor: '0,20' }],
        servicos: [],
      },
      core: {
        core_medias_aparadas: [],
        core_dp: [],
      },
      precos: {
        itens_livres: [],
        transacionaveis: [],
        nao_transacionaveis: [],
        administrados: [],
      },
    }
    mockFetch.mockResolvedValue({ ok: true, json: () => Promise.resolve(apiData) })
    renderPage()
    await waitFor(() => expect(screen.getByText('Grupos de Despesa')).toBeInTheDocument())
  })

  it('switches tabs, toggles series, and toggles IPCA Total', async () => {
    const apiData = {
      grupos: {
        alimentacao_bebidas: [
          { data: '01/06/2025', valor: '0,48' },
          { data: '01/07/2025', valor: '0,50' },
        ],
        habitacao: [{ data: '01/07/2025', valor: '0,30' }],
        artigos_residencia: [],
        vestuario: [],
        transportes: [],
        comunicacao: [],
        saude_cuidados: [],
        despesas_pessoais: [],
      },
      naturezas: {
        bens_duraveis: [{ data: '01/07/2025', valor: '0,12' }],
        bens_semi_duraveis: [],
        bens_nao_duraveis: [],
        servicos: [],
      },
      core: {
        core_medias_aparadas: [],
        core_dp: [],
      },
      precos: {
        itens_livres: [],
        transacionaveis: [],
        nao_transacionaveis: [],
        administrados: [],
      },
    }
    const ipcaData = { data: [
      { data: '01/06/2025', valor: '0,40' },
      { data: '01/07/2025', valor: '0,42' },
    ] }
    mockFetch.mockImplementation((url: string) => {
      if (url.includes('inflacao')) return Promise.resolve({ ok: true, json: () => Promise.resolve(ipcaData) })
      return Promise.resolve({ ok: true, json: () => Promise.resolve(apiData) })
    })
    renderPage()
    await waitFor(() => expect(screen.getByText('Naturezas')).toBeInTheDocument())

    fireEvent.click(screen.getByText('Naturezas'))
    fireEvent.click(screen.getByText('Core'))
    fireEvent.click(screen.getByText('Livres vs Administrados'))
    fireEvent.click(screen.getByText('Grupos de Despesa'))

    const checkboxes = screen.getAllByRole('checkbox')
    if (checkboxes.length > 0) {
      fireEvent.click(checkboxes[0])
      fireEvent.click(checkboxes[0])
    }

    fireEvent.click(screen.getByText('Todos'))
    fireEvent.click(screen.getByText('Nenhum'))

    fireEvent.click(screen.getByText('IPCA Total'))
    fireEvent.click(screen.getByText('IPCA Total'))
  })
})
