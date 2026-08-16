import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest'
import { render, screen, within } from '@testing-library/react'
import App from '../App'

beforeEach(() => {
  vi.stubGlobal('fetch', vi.fn().mockResolvedValue({ ok: true, json: () => Promise.resolve({ status: 'running', cache: {} }) }))
})
afterEach(() => vi.restoreAllMocks())

describe('App', () => {
  it('renders without crashing', () => {
    render(<App />)
    expect(screen.getAllByText('X-BRAY').length).toBeGreaterThanOrEqual(1)
  })

  it('renders dashboard by default', () => {
    render(<App />)
    expect(screen.getByText('Raio-X do Macro Brasileiro')).toBeInTheDocument()
  })

  it('renders all nav sections', () => {
    render(<App />)
    const nav = screen.getByRole('navigation')
    expect(within(nav).getByText('Taxas de Juros')).toBeInTheDocument()
    expect(within(nav).getByText('Inflação')).toBeInTheDocument()
    expect(within(nav).getByText('IPCA Decomposição')).toBeInTheDocument()
    expect(within(nav).getByText('Atividade Econômica')).toBeInTheDocument()
    expect(within(nav).getByText('Câmbio')).toBeInTheDocument()
    expect(within(nav).getByText('Títulos Públicos')).toBeInTheDocument()
    expect(within(nav).getByText('Expectativas FOCUS')).toBeInTheDocument()
    expect(within(nav).getByText('Complementares')).toBeInTheDocument()
  })
})
