import { act, fireEvent, render, screen, waitFor, within } from '@testing-library/react'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { MemoryRouter } from 'react-router-dom'

import Layout from '../components/Layout'

const mockFetch = vi.fn<typeof fetch>()

function renderLayout(route = '/') {
  return render(
    <MemoryRouter initialEntries={[route]}>
      <Layout />
    </MemoryRouter>,
  )
}

beforeEach(() => {
  mockFetch.mockReset()
  mockFetch.mockResolvedValue(Response.json({}))
  vi.stubGlobal('fetch', mockFetch)
})

afterEach(() => {
  vi.useRealTimers()
  vi.unstubAllGlobals()
  vi.restoreAllMocks()
})

describe('Layout branch behavior', () => {
  it('opens the navigation from its accessible menu control', () => {
    // Given
    renderLayout()
    const openButton = screen.getByRole('button', { name: 'Abrir menu' })

    // When
    fireEvent.click(openButton)

    // Then
    expect(openButton).toHaveAccessibleName('Fechar menu')
    expect(openButton).toHaveAttribute('aria-controls', 'primary-navigation-drawer')
    expect(openButton).toHaveAttribute('aria-expanded', 'true')
    expect(document.querySelector('.sidebar')).toHaveClass('open')
    expect(document.querySelector('.sidebar-backdrop')).toHaveClass('visible')
  })

  it('closes the open navigation from a control inside the drawer', () => {
    // Given
    renderLayout()
    fireEvent.click(screen.getByRole('button', { name: 'Abrir menu' }))
    const drawer = screen.getByRole('complementary')
    const closeButton = within(drawer).getByRole('button', { name: 'Fechar menu' })

    // When
    fireEvent.click(closeButton)

    // Then
    expect(screen.getByRole('button', { name: 'Abrir menu' })).toBeInTheDocument()
    expect(drawer).not.toHaveClass('open')
    expect(document.querySelector('.sidebar-backdrop')).not.toHaveClass('visible')
  })

  it('uses the dashboard title for an unknown route', () => {
    // Given / When
    renderLayout('/unknown')

    // Then
    expect(screen.getByRole('banner')).toHaveTextContent('Dashboard')
  })

  it('ignores an invalid status timestamp', async () => {
    // Given
    mockFetch.mockResolvedValueOnce(Response.json({ last_updated: 'not-a-date' }))

    // When
    renderLayout()
    await waitFor(() => expect(mockFetch).toHaveBeenCalledWith('/api/status'))

    // Then
    expect(screen.queryByText(/Última atualização:/)).not.toBeInTheDocument()
  })

  it('does not update status after the layout unmounts', async () => {
    // Given
    let resolveStatus: ((response: Response) => void) | undefined
    mockFetch.mockImplementationOnce(() => new Promise<Response>((resolve) => {
      resolveStatus = resolve
    }))
    const view = renderLayout()

    // When
    view.unmount()
    resolveStatus?.(Response.json({ last_updated: '2026-09-02T12:00:00' }))
    await act(async () => Promise.resolve())

    // Then
    expect(screen.queryByText(/Última atualização:/)).not.toBeInTheDocument()
  })

  it('keeps polling through empty and failed status responses', async () => {
    // Given
    mockFetch
      .mockResolvedValueOnce(Response.json({ last_updated: '2026-01-01T00:00:00' }))
      .mockResolvedValueOnce(Response.json({ status: 'ok' }))
      .mockResolvedValueOnce(Response.json({}))
      .mockRejectedValueOnce(new TypeError('temporarily offline'))
      .mockResolvedValueOnce(Response.json({ last_updated: '2999-01-01T00:00:00' }))
    renderLayout()
    await screen.findByText(/Última atualização:/)
    vi.useFakeTimers()

    // When
    fireEvent.click(screen.getByRole('button', { name: 'Refresh Agora' }))
    await act(async () => {
      await vi.advanceTimersByTimeAsync(6000)
    })

    // Then
    expect(mockFetch).toHaveBeenCalledTimes(5)
    expect(screen.getByText(/01\/01\/2999/)).toBeInTheDocument()
  })
})
