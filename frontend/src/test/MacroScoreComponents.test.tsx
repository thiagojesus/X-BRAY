import { fireEvent, render, screen, within } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { classificationLabel, numberLabel, reasonLabel, unitLabel } from '../components/macro-score/format'
import { currentScoreMonth, historyMonths, historyPoints } from '../components/macro-score/history'
import { ScoreComponent, ScoreDimensions } from '../components/macro-score/ScoreDimensions'
import { ScoreHistory } from '../components/macro-score/ScoreHistory'
import { ScoreMethodology } from '../components/macro-score/ScoreMethodology'
import { macroScoreHistorySchema, macroScoreResponseSchema } from '../components/macro-score/schema'
import { macroScoreFixture, macroScoreHistoryFixture, readySnapshot } from './macroScoreFixtures'

beforeEach(() => { vi.stubGlobal('fetch', vi.fn<typeof fetch>().mockResolvedValue(Response.json(macroScoreHistoryFixture))) })
afterEach(() => { vi.unstubAllGlobals(); vi.restoreAllMocks() })

describe('Macro Score components and API boundary', () => {
  it.each([
    { ...macroScoreFixture, data: { ...readySnapshot, overall_coverage: 2 } },
    { ...macroScoreFixture, data: { ...readySnapshot, as_of: 'not-a-date' } },
    { ...macroScoreFixture, data: { ...readySnapshot, score: Number.NaN } },
    { ...macroScoreFixture, data: { ...readySnapshot, dimensions: { activity: readySnapshot.dimensions.activity } } },
    { ...macroScoreFixture, data: { ...readySnapshot, components: {} } },
    { ...macroScoreFixture, data: { ...readySnapshot, components: Object.fromEntries(Object.entries(readySnapshot.components).filter(([key]) => key !== 'desemprego')), dimensions: { ...readySnapshot.dimensions, activity: { ...readySnapshot.dimensions.activity, total_components: 2, components: ['ibc_br_sa_yoy', 'rendimento_real_yoy'] } } } },
    { status: 'unavailable', data: readySnapshot },
    { status: 'ok', data: null },
  ])('rejects malformed or inconsistent API fields when parsing JSON data', payload => {
    // Given / When
    const result = macroScoreResponseSchema.safeParse(payload)
    // Then
    expect(result.success).toBe(false)
  })

  it('freezes nested API values when parsing a valid snapshot', () => {
    // Given / When
    const payload = macroScoreResponseSchema.parse(macroScoreFixture)
    // Then
    expect(Object.isFrozen(payload)).toBe(true)
    expect(Object.isFrozen(payload.data?.components.desemprego.inputs.desemprego.metadata)).toBe(true)
  })

  it('accepts persisted null scores but rejects malformed history envelopes', () => {
    // Given / When
    const parsed = macroScoreHistorySchema.parse(macroScoreHistoryFixture)
    // Then
    expect(parsed.data[1]?.score).toBeNull()
    expect(macroScoreHistorySchema.safeParse({ status: 'unavailable', data: [readySnapshot] }).success).toBe(false)
    expect(macroScoreHistorySchema.safeParse({ status: 'ok', data: [] }).success).toBe(false)
  })

  it('reaches component inputs in two disclosure activations from a dimension card', async () => {
    // Given
    const user = userEvent.setup()
    render(<ScoreDimensions snapshot={readySnapshot} />)
    const dimension = screen.getByRole('article', { name: 'Atividade' })
    // When
    await user.click(within(dimension).getByText('Ver componentes de Atividade'))
    await user.click(within(dimension).getByText('Insumos e método: Desemprego'))
    // Then
    const input = within(dimension).getByRole('region', { name: 'Insumo desemprego' })
    expect(input).toBeVisible()
    expect(input).toHaveTextContent('6,2 % (pct)')
    expect(input).toHaveTextContent('BCB SGS')
    expect(input).toHaveTextContent('2026-08-01')
    expect(input).toHaveTextContent('28/09/2026, 10:00:00 UTC')
    expect(input).toHaveTextContent('first_seen_at')
    expect(input).toHaveTextContent('24369')
  })

  it('shows all component metrics and scalar metadata without recomputing freshness', () => {
    // Given
    const component = readySnapshot.components.reservas_internacionais
    // When
    render(<ScoreComponent component={component} />)
    fireEvent.click(screen.getByText('Insumos e método: Reservas internacionais'))
    // Then
    expect(screen.getByText('360.000 US$ milhões')).toBeVisible()
    expect(screen.getByText('61,25')).toBeVisible()
    expect(screen.getByText('38,75')).toBeVisible()
    expect(screen.getByText('Maior é mais favorável')).toBeVisible()
    expect(screen.getByText(/Sem flag separada na API/)).toBeVisible()
    expect(screen.getByText('true')).toBeVisible()
    expect(screen.getByText('Não informado')).toBeVisible()
    expect(screen.getByText('history-fixture-602')).toBeVisible()
  })

  it('shows a persisted missing reason and no fabricated inputs when the backend marks stale input', () => {
    // Given
    const component = { ...readySnapshot.components.desemprego, score: null, percentile: null, value: null, available_at: null, reference_period: null, reason: 'stale_input', inputs: {} }
    // When
    render(<ScoreComponent component={component} />)
    fireEvent.click(screen.getByText('Insumos e método: Desemprego'))
    // Then
    expect(screen.getByText('Vencido no corte (backend)')).toBeVisible()
    expect(screen.getByText('Insumo vencido no corte (stale_input)')).toBeVisible()
    expect(screen.getByText(/Nenhum insumo disponível registrado/)).toBeVisible()
    expect(screen.getByText('Menor é mais favorável')).toBeVisible()
  })

  it('labels component coverage as partial even when a dimension score is available', () => {
    // Given
    const snapshot = { ...readySnapshot, dimensions: { ...readySnapshot.dimensions, activity: { ...readySnapshot.dimensions.activity, valid_components: 2, coverage: 2 / 3 } } }
    // When
    render(<ScoreDimensions snapshot={snapshot} />)
    // Then
    expect(within(screen.getByRole('article', { name: 'Atividade' })).getByText('Cobertura parcial')).toBeVisible()
  })

  it('opens and closes methodology with focus preserved when no snapshot is available', async () => {
    // Given
    const user = userEvent.setup()
    render(<ScoreMethodology snapshot={null} />)
    const summary = screen.getByText('Metodologia Macro Score Beta')
    // When
    await user.tab()
    await user.keyboard('[Enter]')
    await user.keyboard('[Enter]')
    // Then
    expect(summary).toHaveFocus()
    expect(summary.closest('details')).not.toHaveAttribute('open')
  })

  it('renders complete snapshot audit metadata when methodology is expanded', () => {
    // Given
    render(<ScoreMethodology snapshot={readySnapshot} />)
    // When
    fireEvent.click(screen.getByText('Metodologia Macro Score Beta'))
    // Then
    const region = screen.getByRole('region', { name: 'Proveniência do snapshot' })
    expect(region).toHaveTextContent('30/09/2026, 12:00:02 UTC')
    expect(region).toHaveTextContent(readySnapshot.inputs_hash)
    expect(region).toHaveTextContent(readySnapshot.calculation_cycle_id)
    expect(region).toHaveTextContent('test_fixture')
  })

  it('keeps missing months and null scores as distinct gaps when building the calendar axis', () => {
    // Given
    const months = historyMonths('2026-09')
    // When
    const points = historyPoints(months, macroScoreHistoryFixture.data, 'overall')
    // Then
    expect(months).toHaveLength(120)
    expect(months[0]).toBe('2016-10')
    expect(months[119]).toBe('2026-09')
    expect(points.find(point => point.month === '2026-07')?.value).toBeNull()
    expect(points.find(point => point.month === '2026-07')?.snapshot).not.toBeNull()
    expect(points.find(point => point.month === '2026-08')).toEqual({ month: '2026-08', snapshot: null, value: null })
    expect(points.find(point => point.month === '2026-09')?.value).toBe(73.125)
    expect(historyPoints(months, macroScoreHistoryFixture.data, 'activity')[119]?.value).toBe(61.25)
  })

  it('keeps retrospective-compatible month windows at 120 inclusive across leap-year rollover', () => {
    const months = historyMonths('2024-02')
    expect(months).toHaveLength(120)
    expect(months[0]).toBe('2014-03')
    expect(months[months.length - 1]).toBe('2024-02')
    expect(months.filter(month => month.startsWith('2024-')).slice(0, 2)).toEqual(['2024-01', '2024-02'])
  })

  it('shows gaps and persisted dimension scores in accessible values when a series is selected', async () => {
    // Given
    render(<ScoreHistory endMonth="2026-09" />)
    await screen.findByText('Valores acessíveis: Score geral')
    // When
    fireEvent.change(screen.getByRole('combobox', { name: 'Série do histórico' }), { target: { value: 'inflation' } })
    fireEvent.click(screen.getByText('Valores acessíveis: Inflação'))
    // Then
    const table = screen.getByRole('table')
    expect(screen.getByRole('heading', { name: 'Histórico: Inflação' })).toBeVisible()
    expect(within(table).getByText('2026-08').closest('tr')).toHaveTextContent('Sem snapshot')
    expect(within(table).getByText('2026-09').closest('tr')).toHaveTextContent('50')
    expect(screen.getByText(/Unidade: pontos de 0 a 100/)).toBeVisible()
  })

  it.each([
    { month: '2026-09', score: '73,125', state: 'Disponível', cutoff: '30/09/2026, 12:00:00 UTC' },
    { month: '2026-07', score: 'Indisponível', state: 'Score indisponível', cutoff: '30/09/2026, 12:00:00 UTC' },
    { month: '2026-08', score: 'Indisponível', state: 'Sem snapshot', cutoff: 'Não disponível' },
  ])('shows labeled mobile history fields for $month when persisted and missing months are disclosed', async ({ month, score, state, cutoff }) => {
    // Given
    render(<ScoreHistory endMonth="2026-09" />)
    await screen.findByText('Valores acessíveis: Score geral')
    // When
    fireEvent.click(screen.getByText('Valores acessíveis: Score geral'))
    // Then
    const list = within(screen.getByRole('region', { name: 'Histórico persistido' })).getByRole('list')
    expect(['UL', 'OL']).toContain(list.tagName)
    const entry = within(list).getByText(month, { exact: true }).closest('li')
    if (entry === null) throw new TypeError('History months must belong to native list items')
    expect(within(entry).getAllByRole('term').map(term => term.textContent)).toEqual(['Mês-base', 'Score', 'Estado', 'Corte de informação (as_of)'])
    expect(within(entry).getAllByRole('definition').map(value => value.textContent)).toEqual([month, score, state, cutoff])
  })

  it('keeps all 120 mobile history months in calendar order when persisted history has gaps', async () => {
    // Given
    const months = Array.from({ length: 120 }, (_, index) => `${2016 + Math.floor((index + 9) / 12)}-${String((index + 9) % 12 + 1).padStart(2, '0')}`)
    render(<ScoreHistory endMonth="2026-09" />)
    await screen.findByText('Valores acessíveis: Score geral')
    // When
    fireEvent.click(screen.getByText('Valores acessíveis: Score geral'))
    // Then
    const history = within(screen.getByRole('region', { name: 'Histórico persistido' }))
    const entries = within(history.getByRole('list')).getAllByRole('listitem')
    expect(entries).toHaveLength(120)
    expect(entries.map(entry => within(entry).getAllByRole('definition')[0]?.textContent)).toEqual(months)
    expect(within(history.getByRole('table')).getAllByRole('rowheader').map(header => header.textContent)).toEqual(months)
  })

  it('uses the selected persisted dimension in mobile history when the series changes', async () => {
    // Given
    render(<ScoreHistory endMonth="2026-09" />)
    await screen.findByText('Valores acessíveis: Score geral')
    fireEvent.click(screen.getByText('Valores acessíveis: Score geral'))
    // When
    fireEvent.change(screen.getByRole('combobox', { name: 'Série do histórico' }), { target: { value: 'inflation' } })
    // Then
    const history = within(screen.getByRole('region', { name: 'Histórico persistido' }))
    const list = history.getByRole('list')
    const entry = within(list).getByText('2026-09', { exact: true }).closest('li')
    if (entry === null) throw new TypeError('History months must belong to native list items')
    expect(screen.getByText('Valores acessíveis: Inflação')).toBeVisible()
    expect(within(entry).getAllByRole('definition').map(value => value.textContent)).toEqual(['2026-09', '50', 'Disponível', '30/09/2026, 12:00:00 UTC'])
    expect(within(history.getByRole('table')).getByText('2026-09').closest('tr')).toHaveTextContent('50')
  })

  it('announces when persisted snapshots contain no points for the selected series', async () => {
    // Given
    const unavailableHistory = macroScoreHistorySchema.parse({
      status: 'ok',
      data: [{ ...readySnapshot, score: null, classification: null }],
    })
    vi.stubGlobal('fetch', vi.fn<typeof fetch>().mockResolvedValue(Response.json(unavailableHistory)))
    // When
    render(<ScoreHistory endMonth="2026-09" />)
    // Then
    expect(await screen.findByText(/Nenhum score disponível para esta série/)).toBeVisible()
    expect(screen.queryByTestId('recharts-container')).not.toBeInTheDocument()
    expect(screen.getByText('Valores acessíveis: Score geral')).toBeVisible()
  })

  it('uses Sao Paulo month boundaries rather than UTC when requesting calendar history', () => {
    // Given / When
    const month = currentScoreMonth(new Date('2026-01-01T01:00:00Z'))
    // Then
    expect(month).toBe('2025-12')
    expect(numberLabel(0)).toBe('0')
    expect(Number(numberLabel(1e-24).replace(',', '.'))).toBe(1e-24)
    expect(classificationLabel(null)).toBe('Sem classificação')
    expect(reasonLabel('new_backend_reason')).toBe('new_backend_reason')
    expect(unitLabel('index')).toBe('index')
  })
})
