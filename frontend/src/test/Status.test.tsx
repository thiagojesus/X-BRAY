import { describe, it, expect } from 'vitest'
import { render, screen } from '@testing-library/react'
import { Loading, ErrorDisplay, EmptyState } from '../components/Status'

describe('Loading', () => {
  it('renders loading message', () => {
    render(<Loading />)
    expect(screen.getByText('Carregando dados...')).toBeInTheDocument()
  })

  it('has spinner element', () => {
    const { container } = render(<Loading />)
    expect(container.querySelector('.spinner')).toBeInTheDocument()
  })
})

describe('ErrorDisplay', () => {
  it('renders error message', () => {
    render(<ErrorDisplay message="Falha na rede" />)
    expect(screen.getByText('Erro ao carregar dados: Falha na rede')).toBeInTheDocument()
  })
})

describe('EmptyState', () => {
  it('renders default message', () => {
    render(<EmptyState />)
    expect(screen.getByText('Sem dados disponíveis')).toBeInTheDocument()
  })

  it('renders custom message', () => {
    render(<EmptyState message="Nada aqui" />)
    expect(screen.getByText('Nada aqui')).toBeInTheDocument()
  })
})
