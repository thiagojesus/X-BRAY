import { afterEach, describe, expect, it, vi } from 'vitest'
import { authFromSessionResponse, fetchSession, nextExpiry, postAuthAction } from '../admin/authClient'

const session = () => ({
  user_id: '11111111-1111-4111-8111-111111111111', permissions: ['monitoring:read'],
  idle_expires_at: new Date(Date.now() + 60_000).toISOString(),
  absolute_expires_at: new Date(Date.now() + 120_000).toISOString(), csrf_token: 'c'.repeat(43),
})

afterEach(() => vi.unstubAllGlobals())

describe('admin auth protocol client', () => {
  it('validates the minimal DTO and maps auth failures without retaining data', () => {
    expect(authFromSessionResponse(200, session()).status).toBe('authenticated')
    expect(authFromSessionResponse(200, { ...session(), permissions: ['admin'] }).status).toBe('unavailable')
    expect(authFromSessionResponse(200, { ...session(), extra: 'private' }).status).toBe('unavailable')
    expect(authFromSessionResponse(200, { ...session(), idle_expires_at: new Date(Date.now() + 200_000).toISOString() }).status).toBe('unavailable')
    expect(authFromSessionResponse(401, session())).toEqual({ status: 'unauthenticated', session: null })
    expect(authFromSessionResponse(403, session())).toEqual({ status: 'forbidden', session: null })
    expect(authFromSessionResponse(503, session())).toEqual({ status: 'unavailable', session: null })
  })

  it('GETs only same-origin no-store session data and handles malformed or failed responses', async () => {
    const fetchMock = vi.fn()
      .mockResolvedValueOnce(Response.json(session()))
      .mockResolvedValueOnce(new Response('invalid', { status: 200 }))
      .mockRejectedValueOnce(new Error('network'))
    vi.stubGlobal('fetch', fetchMock)
    expect((await fetchSession()).status).toBe('authenticated')
    expect(fetchMock.mock.calls[0][0]).toBe('/api/auth/session')
    expect(fetchMock.mock.calls[0][1]).toMatchObject({ credentials: 'same-origin', cache: 'no-store', method: 'GET' })
    expect((await fetchSession()).status).toBe('unavailable')
    expect((await fetchSession()).status).toBe('unavailable')
    const valid = authFromSessionResponse(200, session())
    expect(valid.session).not.toBeNull()
    if (valid.session) expect(nextExpiry(valid.session)).toBeLessThanOrEqual(Date.parse(session().absolute_expires_at))
  })

  it('sends CSRF only in the request header for actions and never exposes error response text', async () => {
    const fetchMock = vi.fn()
      .mockResolvedValueOnce(Response.json({ ok: true }))
      .mockResolvedValueOnce(Response.json({ error: 'request_forbidden', detail: 'do not show' }, { status: 403 }))
      .mockResolvedValueOnce(new Response('not-json', { status: 500 }))
      .mockRejectedValueOnce(new Error('offline'))
    vi.stubGlobal('fetch', fetchMock)
    expect(await postAuthAction('activity', 'csrf-synthetic')).toEqual({ status: 200, error: null })
    const init = fetchMock.mock.calls[0][1] as RequestInit
    expect(init).toMatchObject({ method: 'POST', credentials: 'same-origin', cache: 'no-store', body: '{}' })
    expect(new Headers(init.headers).get('X-CSRF-Token')).toBe('csrf-synthetic')
    expect(await postAuthAction('logout', 'csrf-synthetic')).toEqual({ status: 403, error: 'request_forbidden' })
    expect(await postAuthAction('logout', 'csrf-synthetic')).toEqual({ status: 500, error: null })
    expect(await postAuthAction('logout', 'csrf-synthetic')).toEqual({ status: 0, error: null })
  })
})
