import { render, screen, within } from '@testing-library/react'
import type { ReactNode } from 'react'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'

import { SidraSections } from '../components/SidraSections'

const mockFetch = vi.fn()

vi.mock('recharts', () => ({
  CartesianGrid: () => null,
  Legend: () => null,
  Line: () => null,
  LineChart: ({ children, data }: {
    readonly children: ReactNode
    readonly data: readonly Record<string, number | string>[]
  }) => (
    <div data-testid="sidra-line-chart">
      <ol>
        {data.map(row => <li key={String(row.period)}>{String(row.period)}</li>)}
      </ol>
      {children}
    </div>
  ),
  ResponsiveContainer: ({ children }: { readonly children: ReactNode }) => children,
  Tooltip: () => null,
  XAxis: () => null,
  YAxis: () => null,
}))

const fullPayload = {
  source: 'IBGE SIDRA',
  status: 'ok',
  last_updated: '2026-09-27T12:00:00Z',
  unavailable: [],
  data: {
    pim_industria: [{ reference_period: '202607', period_label: 'julho 2026', value: 104.00233, available_at: '2026-09-27T12:00:00Z', payload_hash: 'pim' }],
    pmc_varejo_ampliado: [{ reference_period: '202607', period_label: 'julho 2026', value: 106.86857, available_at: '2026-09-27T12:00:00Z', payload_hash: 'pmc' }],
    pms_servicos: [{ reference_period: '202607', period_label: 'julho 2026', value: 110.5941, available_at: '2026-09-27T12:00:00Z', payload_hash: 'pms' }],
    massa_rendimento_real: [{ reference_period: '202607', period_label: 'mai-jun-jul 2026', value: 383472, available_at: '2026-09-27T12:00:00Z', payload_hash: 'massa' }],
    taxa_subutilizacao: [{ reference_period: '202607', period_label: 'mai-jun-jul 2026', value: 13, available_at: '2026-09-27T12:00:00Z', payload_hash: 'sub' }],
  },
  metadata: {
    pim_industria: { table_id: 8888, variable_id: 12607, unit: 'index', unit_label: 'Número-índice', base_version: '2022=100', frequency: 'monthly', period_label: 'julho 2026' },
    pmc_varejo_ampliado: { table_id: 8881, variable_id: 7170, unit: 'index', unit_label: 'Número-índice', base_version: '2022=100', frequency: 'monthly', period_label: 'julho 2026' },
    pms_servicos: { table_id: 5906, variable_id: 7168, unit: 'index', unit_label: 'Número-índice', base_version: '2022=100', frequency: 'monthly', period_label: 'julho 2026' },
    massa_rendimento_real: { table_id: 6392, variable_id: 6293, unit: 'brl_million', unit_label: 'Milhões de Reais', base_version: 'real_deflator_latest_published_moving_quarter', frequency: 'moving_quarter', period_label: 'mai-jun-jul 2026' },
    taxa_subutilizacao: { table_id: 6441, variable_id: 4118, unit: 'pct', unit_label: 'Percentual', base_version: null, frequency: 'moving_quarter', period_label: 'mai-jun-jul 2026' },
  },
} as const

beforeEach(() => {
  vi.stubGlobal('fetch', mockFetch)
})

afterEach(() => vi.restoreAllMocks())

describe('SidraSections', () => {
  it('renders five approved datasets with explicit units, bases, and period semantics', async () => {
    mockFetch.mockResolvedValue(Response.json(fullPayload))

    render(<SidraSections />)

    expect(await screen.findByText('Aprofundamento IBGE/SIDRA')).toBeInTheDocument()
    expect(screen.getByText('Produção industrial')).toBeInTheDocument()
    expect(screen.getByText('Varejo ampliado')).toBeInTheDocument()
    expect(screen.getByText('Volume de serviços')).toBeInTheDocument()
    expect(screen.getByText('Massa de rendimento real')).toBeInTheDocument()
    expect(screen.getByText('Taxa de subutilização')).toBeInTheDocument()
    expect(screen.getAllByText(/2022=100/).length).toBeGreaterThan(0)
    expect(screen.getAllByText(/Mensal/).length).toBeGreaterThan(0)
    expect(screen.getAllByText(/Trimestre móvel encerrado em mai-jun-jul 2026/).length).toBeGreaterThan(0)
    expect(screen.getByText('R$ 383.472,0 mi')).toBeInTheDocument()
    expect(screen.getByText('13,0%')).toBeInTheDocument()
    expect(document.querySelectorAll('.chart-container')).toHaveLength(3)
    const charts = screen.getAllByTestId('sidra-line-chart')
    expect(within(charts[1]).getAllByRole('listitem')).toHaveLength(1)
    expect(document.body).not.toHaveTextContent(/NaN|Infinity/)
  })

  it('orders localized month labels by chronological reference period', async () => {
    mockFetch.mockResolvedValue(Response.json({
      ...fullPayload,
      data: {
        ...fullPayload.data,
        pim_industria: [
          { ...fullPayload.data.pim_industria[0], reference_period: '202601', period_label: 'janeiro 2026' },
          { ...fullPayload.data.pim_industria[0], reference_period: '202602', period_label: 'fevereiro 2026' },
        ],
      },
    }))

    render(<SidraSections />)

    const industrialChart = (await screen.findAllByTestId('sidra-line-chart'))[0]
    expect(within(industrialChart).getAllByRole('listitem').map(item => item.textContent)).toEqual([
      'janeiro 2026',
      'fevereiro 2026',
    ])
  })

  it('renders a usable subset as partial and names unavailable datasets', async () => {
    mockFetch.mockResolvedValue(Response.json({
      ...fullPayload,
      status: 'partial',
      unavailable: ['pmc_varejo_ampliado', 'pms_servicos', 'massa_rendimento_real', 'taxa_subutilizacao'],
      data: { pim_industria: fullPayload.data.pim_industria },
      metadata: { pim_industria: fullPayload.metadata.pim_industria },
    }))

    render(<SidraSections />)

    expect(await screen.findByRole('status')).toHaveTextContent('Fonte parcialmente indisponível.')
    expect(screen.getByText(/Varejo ampliado/)).toBeInTheDocument()
    expect(screen.getByText('Produção industrial')).toBeInTheDocument()
    expect(document.body).not.toHaveTextContent(/NaN|Infinity/)
  })

  it('contains its own provider failure instead of failing the parent page', async () => {
    mockFetch.mockRejectedValue(new Error('SIDRA offline'))

    render(<SidraSections />)

    expect(await screen.findByRole('alert')).toHaveTextContent('Dados SIDRA indisponíveis')
  })
})
