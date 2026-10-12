import { fireEvent, render, screen, within } from '@testing-library/react'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import Derivados from '../pages/Derivados'
import { aggregateExpectations, aggregateFixture, realRateFixture } from './derivedFixtures'

const mockFetch = vi.fn<typeof fetch>()
let aggregateResponse: () => Promise<Response>
let dedicatedResponse: () => Promise<Response>

beforeEach(() => {
  aggregateResponse = async () => Response.json(aggregateFixture)
  dedicatedResponse = async () => Response.json(realRateFixture)
  mockFetch.mockReset()
  mockFetch.mockImplementation(async input => {
    const url = input instanceof Request ? input.url : input.toString()
    if (url === '/api/derivados') return aggregateResponse()
    if (url === '/api/derivados/juro-real-ex-ante-1y') return dedicatedResponse()
    return new Response(null, { status: 404 })
  })
  vi.stubGlobal('fetch', mockFetch)
})

afterEach(() => {
  vi.unstubAllGlobals()
  vi.restoreAllMocks()
})

describe('Derivados additional indicators', () => {
  it.each(aggregateExpectations)('renders $key with persisted value and dates', async ({ title, value }) => {
    // Given

    // When
    render(<Derivados />)

    // Then
    const card = await screen.findByRole('article', { name: title })
    expect(within(card).getByText(value, { exact: true })).toBeVisible()
    expect(within(card).getByText('30/09/2026, 12:00 UTC')).toBeVisible()
    expect(within(card).getByText(/^(01\/08\/2026|25\/09\/2026)$/)).toBeVisible()
    expect(screen.getAllByText('6,84% a.a.').length).toBeGreaterThanOrEqual(1)
  })

  it('retains valid indicators when unavailable and missing siblings coexist', async () => {
    // Given
    const { inclinacao_di_5y_1y: _missing, ...data } = aggregateFixture.data
    aggregateResponse = async () => Response.json({ status: 'ok', data: {
      ...data,
      cobertura_externa: { ...data.cobertura_externa, value: null, reason: 'current_account_non_negative' },
    } })

    // When
    render(<Derivados />)

    // Then
    const coverage = await screen.findByRole('article', { name: aggregateExpectations[0].title })
    expect(within(coverage).getAllByText('Indisponível', { exact: true })).toHaveLength(2)
    expect(within(coverage).getByText('A conta corrente não está em déficit neste snapshot.')).toBeVisible()
    const slope = screen.getByRole('article', { name: aggregateExpectations[4].title })
    expect(within(slope).getByText('Snapshot não encontrado neste cache.')).toBeVisible()
    expect(screen.getByText('10,12 p.p.')).toBeVisible()
    expect(screen.getByText('1,66 p.p.')).toBeVisible()
  })

  it('shows the exact empty-cache state without fabricated indicator cards', async () => {
    // Given
    aggregateResponse = async () => Response.json({ status: 'unavailable', data: {} })

    // When
    render(<Derivados />)

    // Then
    const section = await screen.findByRole('region', { name: 'Outros indicadores derivados' })
    expect(within(section).getByText('Nenhum snapshot dos cinco indicadores está disponível no cache.')).toBeVisible()
    expect(within(section).queryByRole('article')).not.toBeInTheDocument()
    expect(screen.getAllByText('6,84% a.a.').length).toBeGreaterThanOrEqual(1)
  })

  it('shows an aggregate HTTP error without hiding the cached real rate', async () => {
    // Given
    aggregateResponse = async () => new Response(null, { status: 503 })

    // When
    render(<Derivados />)

    // Then
    const section = await screen.findByRole('region', { name: 'Outros indicadores derivados' })
    expect(within(section).getByRole('alert')).toHaveTextContent('HTTP 503')
    expect(within(section).queryByRole('article')).not.toBeInTheDocument()
    expect(screen.getAllByText('6,84% a.a.').length).toBeGreaterThanOrEqual(1)
  })

  it('keeps the last aggregate snapshot visible after a failed refresh', async () => {
    // Given
    render(<Derivados />)
    const section = screen.getByRole('region', { name: 'Outros indicadores derivados' })
    await within(section).findByText('140,00%')
    aggregateResponse = async () => new Response(null, { status: 503 })

    // When
    fireEvent(window, new Event('online'))

    // Then
    expect(await within(section).findByRole('alert')).toHaveTextContent('HTTP 503')
    expect(within(section).getByText('Dados possivelmente desatualizados.')).toBeVisible()
    expect(within(section).getByText('140,00%')).toBeVisible()
  })

  it('rejects a malformed aggregate envelope instead of fabricating indicator data', async () => {
    // Given
    aggregateResponse = async () => Response.json({ status: 'unexpected', data: {} })

    // When
    render(<Derivados />)

    // Then
    const section = screen.getByRole('region', { name: 'Outros indicadores derivados' })
    expect(await within(section).findByRole('alert')).toHaveTextContent('Resposta inválida de /api/derivados.')
    expect(within(section).queryByRole('article')).not.toBeInTheDocument()
  })

  it('renders provenance and keyboard-operable methodology details', async () => {
    // Given
    render(<Derivados />)

    // When
    const card = await screen.findByRole('article', { name: aggregateExpectations[3].title })

    // Then
    const summary = within(card).getByText('Metodologia e insumos')
    expect(summary.tagName).toBe('SUMMARY')
    expect(summary.closest('details')).not.toHaveAttribute('open')
    summary.focus()
    expect(summary).toHaveFocus()
    fireEvent.click(summary)
    expect(summary.closest('details')).toHaveAttribute('open')
    expect(within(card).getByText(aggregateFixture.data.distancia_focus_meta.formula)).toBeVisible()
    expect(within(card).getByText('https://www.bcb.gov.br/controleinflacao/historicometas')).toBeVisible()
    expect(within(card).getAllByText(/Disponível: 27\/09\/2026, 10:00 UTC/)).toHaveLength(2)
    expect(within(card).getAllByText(/Disponível desde: Primeira observação local/)).toHaveLength(2)
    expect(within(card).getByText(/Filtro Focus: Indicador eq 'IPCA' and Suavizada eq 'S' and baseCalculo eq 0/)).toBeVisible()
    expect(within(card).getByText(/Regra aplicada: bcb-continuous-target-2025/)).toBeVisible()
  })

  it('keeps every additional indicator methodology in a focusable native disclosure', async () => {
    render(<Derivados />)

    for (const { title } of aggregateExpectations) {
      const card = await screen.findByRole('article', { name: title })
      const summary = within(card).getByText('Metodologia e insumos')
      summary.focus()
      expect(summary.tagName).toBe('SUMMARY')
      expect(summary).toHaveFocus()
      const details = summary.closest('details')
      expect(details).not.toHaveAttribute('open')
      fireEvent.click(summary)
      expect(details).toHaveAttribute('open')
      if (title === aggregateExpectations[1].title) {
        expect(within(card).getByText('0,15% do PIB')).toBeVisible()
        expect(within(card).getByText('-0,20% do PIB')).toBeVisible()
        expect(within(card).getAllByText(/Código SGS: 5793/)).toHaveLength(2)
      }
      fireEvent.click(summary)
      expect(details).not.toHaveAttribute('open')
    }
  })

  it('isolates a malformed indicator payload from valid siblings', async () => {
    // Given
    const response = Response.json({})
    vi.spyOn(response, 'json').mockResolvedValue({
      status: 'ok',
      data: {
        ...aggregateFixture.data,
        cobertura_externa: {
          ...aggregateFixture.data.cobertura_externa,
          value: Number.NaN,
        },
      },
    })
    aggregateResponse = async () => response

    // When
    render(<Derivados />)

    // Then
    const coverage = await screen.findByRole('article', { name: aggregateExpectations[0].title })
    expect(within(coverage).getByText('Payload inválido.')).toBeVisible()
    expect(within(coverage).queryByText(/NaN|Infinity/)).not.toBeInTheDocument()
    expect(screen.getByText('10,12 p.p.')).toBeVisible()
  })
})
