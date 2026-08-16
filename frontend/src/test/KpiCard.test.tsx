import { describe, it, expect, vi } from 'vitest'
import { render, screen, fireEvent } from '@testing-library/react'
import { KpiCard } from '../components/KpiCard'

describe('KpiCard', () => {
  it('renders name, value, and date', () => {
    render(<KpiCard name="Selic" value="12,25%" date="01/08/2025" color="#ff0000" />)
    expect(screen.getByText('Selic')).toBeInTheDocument()
    expect(screen.getByText('12,25%')).toBeInTheDocument()
    expect(screen.getByText('01/08/2025')).toBeInTheDocument()
  })

  it('shows dash when value is empty', () => {
    render(<KpiCard name="Test" value="" date="" color="#000" />)
    expect(screen.getByText('—')).toBeInTheDocument()
  })

  it('shows info icon when description is provided', () => {
    render(<KpiCard name="Test" value="1" date="01/01/2025" color="#000" description="Tooltip text" />)
    expect(screen.getByText('i')).toBeInTheDocument()
  })

  it('shows tooltip on hover when description is provided', () => {
    render(<KpiCard name="Test" value="1" date="01/01/2025" color="#000" description="Explanation text" />)
    fireEvent.mouseEnter(screen.getByText('Test'))
    expect(screen.getByText('Explanation text')).toBeInTheDocument()
  })

  it('hides tooltip on mouse leave', () => {
    render(<KpiCard name="Test" value="1" date="01/01/2025" color="#000" description="Explanation text" />)
    const label = screen.getByText('Test')
    fireEvent.mouseEnter(label)
    fireEvent.mouseLeave(label)
    expect(screen.queryByText('Explanation text')).not.toBeInTheDocument()
  })

  it('does not show info icon without description', () => {
    render(<KpiCard name="Test" value="1" date="01/01/2025" color="#000" />)
    expect(screen.queryByText('i')).not.toBeInTheDocument()
  })

  it('applies color as border-top', () => {
    const { container } = render(<KpiCard name="Test" value="1" date="01/01/2025" color="#ff6b6b" />)
    expect(container.firstChild).toHaveStyle({ borderTopColor: '#ff6b6b' })
  })
})
