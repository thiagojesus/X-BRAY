import { describe, expect, it, vi } from 'vitest'
import { render, screen, waitFor } from '@testing-library/react'
import { DataQualityNotice } from '../components/DataQualityNotice'

describe('DataQualityNotice', () => {
  it('announces partial cached data and its last update', async () => {
    vi.mocked(fetch).mockResolvedValueOnce({
      ok: true,
      json: async () => ({
        status: 'partial',
        last_updated: '2026-09-12T12:30:00Z',
        metadata: {},
      }),
    } as Response)
    render(<DataQualityNotice dataset="atividade" />)
    const notice = await screen.findByRole('status')
    expect(notice).toHaveTextContent('Fonte parcialmente indisponível.')
    expect(notice).toHaveTextContent('Última atualização:')
    expect(fetch).toHaveBeenCalledWith('/api/data-quality/atividade', expect.objectContaining({ signal: expect.any(AbortSignal) }))
  })

  it('handles failure states without discarding retained legacy data', async () => {
    vi.mocked(fetch).mockRejectedValueOnce(new Error('offline'))
    render(<DataQualityNotice dataset="atividade" isStale />)
    await waitFor(() => expect(screen.getByRole('status')).toHaveTextContent('Fonte indisponível.'))
  })
})
