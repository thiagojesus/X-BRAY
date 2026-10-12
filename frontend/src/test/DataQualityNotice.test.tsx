import { describe, expect, it, vi } from 'vitest'
import { render, screen } from '@testing-library/react'
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

  it('[RED XBR-102] announces stale available data without calling its source unavailable', async () => {
    render(<DataQualityNotice dataset="fiscal" quality={{
      status: 'ok',
      last_updated: '2026-09-12T12:30:00Z',
      metadata: { dbgg_pib: { freshness: 'stale' } },
    }} />)

    const notice = screen.getByRole('status')
    expect(notice).toHaveTextContent('Dados possivelmente desatualizados.')
    expect(notice).not.toHaveTextContent('indisponível')
  })
})
