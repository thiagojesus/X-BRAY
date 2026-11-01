import { fireEvent, render, screen, waitFor } from '@testing-library/react'
import { MemoryRouter, Route, Routes } from 'react-router-dom'
import { afterEach, describe, expect, it, vi } from 'vitest'
import { AdminGate } from '../admin/AdminArea'
import { AdminAuthProvider, useAdminAuth } from '../admin/AuthProvider'
import LoginPage, { beginGoogleLogin } from '../admin/LoginPage'
import type { AuthSnapshot } from '../admin/authClient'

const session = (csrf = 'c'.repeat(43)) => ({
  user_id: '11111111-1111-4111-8111-111111111111', permissions: ['monitoring:read'],
  idle_expires_at: new Date(Date.now() + 300_000).toISOString(),
  absolute_expires_at: new Date(Date.now() + 600_000).toISOString(), csrf_token: csrf,
})

function setup(path: string, navigateToLogin?: () => void) {
  const originalFetch = globalThis.fetch
  vi.stubGlobal('fetch', vi.fn((input: RequestInfo | URL, init?: RequestInit) => {
    if (String(input).startsWith('/api/admin/')) return Promise.resolve(Response.json({ error: 'unavailable' }, { status: 503 }))
    return originalFetch(input, init)
  }))
  return render(<MemoryRouter initialEntries={[path]}><Routes>
    <Route path="/login" element={<AdminAuthProvider><LoginPage navigateToLogin={navigateToLogin} /></AdminAuthProvider>} />
    <Route path="/admin" element={<AdminAuthProvider monitorSession><AdminGate /></AdminAuthProvider>} />
  </Routes></MemoryRouter>)
}

const originalVisibilityDescriptor = Object.getOwnPropertyDescriptor(document, 'visibilityState')
afterEach(() => {
  vi.unstubAllGlobals()
  vi.useRealTimers()
  if (originalVisibilityDescriptor) Object.defineProperty(document, 'visibilityState', originalVisibilityDescriptor)
  else Reflect.deleteProperty(document, 'visibilityState')
})

function AuthProbe({ onRefreshResult }: { onRefreshResult?: (snapshot: AuthSnapshot) => void } = {}) {
  const { auth, refresh, logout } = useAdminAuth()
  return <><output>{auth.status}:{auth.session?.user_id ?? 'none'}</output>
    <button type="button" onClick={() => void refresh().then(onRefreshResult)}>Probe refresh</button>
    <button type="button" onClick={() => void logout()}>Probe logout</button>
    {auth.status === 'authenticated' && <p>Private probe content</p>}
  </>
}

describe('private admin routes', () => {
  it('does not render private content before validating and redirects a 401 to fixed login', async () => {
    vi.stubGlobal('fetch', vi.fn().mockResolvedValue(Response.json({ error: 'session_invalid' }, { status: 401 })))
    setup('/admin')
    expect(screen.getByRole('status')).toHaveTextContent('Verificando acesso')
    expect(await screen.findByRole('heading', { name: 'Área administrativa' })).toBeInTheDocument()
    expect(screen.queryByText('Estado registrado')).not.toBeInTheDocument()
    expect(screen.getByText('Sua sessão expirou. Entre novamente.')).toBeInTheDocument()
  })

  it('guards a direct visit, renders only placeholder, and logs out with same-origin CSRF', async () => {
    const fetchMock = vi.fn()
      .mockResolvedValueOnce(Response.json(session()))
      .mockResolvedValueOnce(Response.json({ ok: true }))
      .mockResolvedValueOnce(Response.json({ error: 'session_invalid' }, { status: 401 }))
    vi.stubGlobal('fetch', fetchMock)
    setup('/admin')
    expect(await screen.findByRole('button', { name: 'Sair' })).toBeInTheDocument()
    fireEvent.click(screen.getByRole('button', { name: 'Sair' }))
    await screen.findByRole('heading', { name: 'Área administrativa' })
    expect(fetchMock.mock.calls[1][0]).toBe('/api/auth/logout')
    expect(new Headers(fetchMock.mock.calls[1][1].headers).get('X-CSRF-Token')).toBe(session().csrf_token)
    expect(fetchMock.mock.calls[1][1]).toMatchObject({ method: 'POST', credentials: 'same-origin', cache: 'no-store' })
  })

  it('distinguishes unavailable session and gives an explicit retry', async () => {
    const fetchMock = vi.fn()
      .mockResolvedValueOnce(Response.json({ error: 'auth_unavailable' }, { status: 503 }))
      .mockResolvedValueOnce(Response.json(session()))
    vi.stubGlobal('fetch', fetchMock)
    setup('/admin')
    expect(await screen.findByText(/Os dados privados permanecem ocultos/)).toBeInTheDocument()
    fireEvent.click(screen.getByRole('button', { name: 'Tentar novamente' }))
    expect(await screen.findByText('Estado registrado')).toBeInTheDocument()
  })

  it('hides protected view immediately when logout cannot be confirmed and permits an explicit retry', async () => {
    const fetchMock = vi.fn()
      .mockResolvedValueOnce(Response.json(session()))
      .mockRejectedValueOnce(new Error('offline'))
      .mockResolvedValueOnce(Response.json(session()))
      .mockResolvedValueOnce(Response.json({ ok: true }))
      .mockResolvedValueOnce(Response.json({ error: 'session_invalid' }, { status: 401 }))
    vi.stubGlobal('fetch', fetchMock)
    setup('/admin')
    expect(await screen.findByText('Estado registrado')).toBeInTheDocument()
    fireEvent.click(screen.getByRole('button', { name: 'Sair' }))
    expect(await screen.findByRole('heading', { name: 'Saída não confirmada' })).toBeInTheDocument()
    expect(screen.queryByText('Estado registrado')).not.toBeInTheDocument()
    expect(screen.getByText(/A sessão do servidor pode continuar ativa/)).toBeInTheDocument()
    fireEvent.click(screen.getByRole('button', { name: 'Tentar sair novamente' }))
    await screen.findByRole('heading', { name: 'Área administrativa' })
    expect(fetchMock.mock.calls.filter(([url]) => url === '/api/auth/logout')).toHaveLength(2)
  })

  it('treats a retry revalidation that finds no session as locally signed out', async () => {
    const fetchMock = vi.fn()
      .mockResolvedValueOnce(Response.json(session()))
      .mockRejectedValueOnce(new Error('offline'))
      .mockResolvedValueOnce(Response.json({ error: 'session_invalid' }, { status: 401 }))
      .mockResolvedValueOnce(Response.json({ error: 'session_invalid' }, { status: 401 }))
    vi.stubGlobal('fetch', fetchMock)
    setup('/admin')
    await screen.findByText('Estado registrado')
    fireEvent.click(screen.getByRole('button', { name: 'Sair' }))
    await screen.findByRole('heading', { name: 'Saída não confirmada' })
    fireEvent.click(screen.getByRole('button', { name: 'Tentar sair novamente' }))
    expect(await screen.findByRole('heading', { name: 'Área administrativa' })).toBeInTheDocument()
    expect(fetchMock.mock.calls.filter(([url]) => url === '/api/auth/logout')).toHaveLength(1)
  })

  it('keeps logout explicitly unconfirmed if retry session validation is unavailable', async () => {
    const fetchMock = vi.fn()
      .mockResolvedValueOnce(Response.json(session()))
      .mockRejectedValueOnce(new Error('offline'))
      .mockResolvedValueOnce(Response.json({ error: 'auth_unavailable' }, { status: 503 }))
    vi.stubGlobal('fetch', fetchMock)
    setup('/admin')
    await screen.findByText('Estado registrado')
    fireEvent.click(screen.getByRole('button', { name: 'Sair' }))
    await screen.findByRole('heading', { name: 'Saída não confirmada' })
    fireEvent.click(screen.getByRole('button', { name: 'Tentar sair novamente' }))
    expect(await screen.findByRole('heading', { name: 'Saída não confirmada' })).toBeInTheDocument()
    expect(screen.queryByText('Estado registrado')).not.toBeInTheDocument()
  })

  it('does not send activity on mount, polling, or while hidden; actual input is throttled', async () => {
    vi.useFakeTimers({ shouldAdvanceTime: true })
    const fetchMock = vi.fn().mockImplementation(async (input: string) => {
      if (input === '/api/auth/activity') return Response.json({ ok: true })
      if (input === '/api/auth/logout') return Response.json({ ok: true })
      return Response.json(session())
    })
    vi.stubGlobal('fetch', fetchMock)
    setup('/admin')
    await screen.findByText('Estado registrado')
    await vi.advanceTimersByTimeAsync(60_000)
    expect(fetchMock.mock.calls.filter(([url]) => url === '/api/auth/activity')).toHaveLength(0)
    Object.defineProperty(document, 'visibilityState', { configurable: true, value: 'hidden' })
    fireEvent(document, new Event('visibilitychange'))
    fireEvent.pointerDown(window)
    expect(fetchMock.mock.calls.filter(([url]) => url === '/api/auth/activity')).toHaveLength(0)
    Object.defineProperty(document, 'visibilityState', { configurable: true, value: 'visible' })
    fireEvent(document, new Event('visibilitychange'))
    await waitFor(() => expect(fetchMock.mock.calls.filter(([url]) => url === '/api/auth/session')).toHaveLength(3))
    fireEvent.pointerDown(window)
    fireEvent.keyDown(window, { key: 'a' })
    await waitFor(() => expect(fetchMock.mock.calls.filter(([url]) => url === '/api/auth/activity')).toHaveLength(1))
  })

  it('renders only fixed Portuguese messages for unknown reason strings', async () => {
    vi.stubGlobal('fetch', vi.fn().mockResolvedValue(Response.json({ error: 'session_invalid' }, { status: 401 })))
    setup('/login?reason=https%3A%2F%2Fattacker.invalid%2Fraw')
    expect(await screen.findByRole('heading', { name: 'Área administrativa' })).toBeInTheDocument()
    expect(screen.queryByText(/attacker\.invalid/)).not.toBeInTheDocument()
    expect(screen.getByRole('button', { name: 'Entrar com Google' })).toBeEnabled()
  })

  it('shows denied account copy for a 403 and never offers provider login', async () => {
    vi.stubGlobal('fetch', vi.fn().mockResolvedValue(Response.json({ error: 'not_authorized' }, { status: 403 })))
    setup('/login?reason=access_denied')
    expect(await screen.findByRole('heading', { name: 'Acesso administrativo' })).toBeInTheDocument()
    expect(screen.getByText('Esta conta não tem acesso administrativo.')).toBeInTheDocument()
    expect(screen.queryByRole('button', { name: 'Entrar com Google' })).not.toBeInTheDocument()
  })

  it('rechecks unavailable login state on explicit retry and enables the fixed Google action', async () => {
    const fetchMock = vi.fn()
      .mockResolvedValueOnce(Response.json({ error: 'auth_unavailable' }, { status: 503 }))
      .mockResolvedValueOnce(Response.json({ error: 'session_invalid' }, { status: 401 }))
    vi.stubGlobal('fetch', fetchMock)
    setup('/login?reason=unrecognized')
    fireEvent.click(await screen.findByRole('button', { name: 'Tentar novamente' }))
    expect(await screen.findByRole('button', { name: 'Entrar com Google' })).toBeEnabled()
    expect(screen.queryByText('unrecognized')).not.toBeInTheDocument()
  })

  it('uses one fixed same-origin provider navigation path', () => {
    const assign = vi.fn()
    beginGoogleLogin(assign)
    expect(assign).toHaveBeenCalledExactlyOnceWith('/api/auth/login')
  })

  it('starts provider navigation once on explicit Google button activation', async () => {
    const fetchMock = vi.fn().mockResolvedValue(Response.json({ error: 'session_invalid' }, { status: 401 }))
    vi.stubGlobal('fetch', fetchMock)
    const navigateToLogin = vi.fn()
    setup('/login', navigateToLogin)
    fireEvent.click(await screen.findByRole('button', { name: 'Entrar com Google' }))
    expect(navigateToLogin).toHaveBeenCalledExactlyOnceWith()
    expect(await screen.findByRole('button', { name: 'Abrindo acesso Google…' })).toBeDisabled()
    fireEvent(window, new Event('pageshow'))
    expect(await screen.findByRole('button', { name: 'Entrar com Google' })).toBeEnabled()
  })

  it('fails closed when the returned session deadlines are already past', async () => {
    vi.stubGlobal('fetch', vi.fn().mockResolvedValue(Response.json({ ...session(), idle_expires_at: new Date(Date.now() - 1000).toISOString(), absolute_expires_at: new Date(Date.now() + 1000).toISOString() })))
    setup('/admin')
    expect(await screen.findByRole('heading', { name: 'Área administrativa indisponível' })).toBeInTheDocument()
    expect(screen.queryByText('Estado registrado')).not.toBeInTheDocument()
  })

  it('navigates a forbidden direct admin session to the fixed denied-account message', async () => {
    vi.stubGlobal('fetch', vi.fn().mockResolvedValue(Response.json({ error: 'not_authorized' }, { status: 403 })))
    setup('/admin')
    expect(await screen.findByRole('heading', { name: 'Acesso administrativo' })).toBeInTheDocument()
    expect(screen.queryByText('Estado registrado')).not.toBeInTheDocument()
  })

  it('revalidates at the local deadline and keeps polling GET-only', async () => {
    vi.useFakeTimers({ shouldAdvanceTime: true })
    const shortSession = { ...session(), idle_expires_at: new Date(Date.now() + 1000).toISOString(), absolute_expires_at: new Date(Date.now() + 2000).toISOString() }
    const fetchMock = vi.fn()
      .mockResolvedValueOnce(Response.json(shortSession))
      .mockResolvedValueOnce(Response.json({ error: 'session_invalid' }, { status: 401 }))
      .mockResolvedValueOnce(Response.json({ error: 'session_invalid' }, { status: 401 }))
    vi.stubGlobal('fetch', fetchMock)
    setup('/admin')
    expect(await screen.findByText('Estado registrado')).toBeInTheDocument()
    await vi.advanceTimersByTimeAsync(1000)
    expect(await screen.findByText('Sua sessão expirou. Entre novamente.')).toBeInTheDocument()
    expect(fetchMock.mock.calls.filter(([url]) => url === '/api/auth/activity')).toHaveLength(0)
  })

  it('clears stale requests and swaps identity only after fresh session validation', async () => {
    const secondSession = { ...session(), user_id: '22222222-2222-4222-8222-222222222222' }
    const fetchMock = vi.fn()
      .mockResolvedValueOnce(Response.json(session()))
      .mockResolvedValueOnce(Response.json(secondSession))
    vi.stubGlobal('fetch', fetchMock)
    render(<MemoryRouter><AdminAuthProvider><AuthProbe /></AdminAuthProvider></MemoryRouter>)
    expect(await screen.findByText('Private probe content')).toBeInTheDocument()
    fireEvent.click(screen.getByRole('button', { name: 'Probe refresh' }))
    expect(await screen.findByText(/authenticated:22222222-/)).toBeInTheDocument()
    expect(fetchMock).toHaveBeenCalledTimes(2)
  })

  it('rechecks a CSRF-rejected logout without claiming success and offers an explicit retry', async () => {
    const fetchMock = vi.fn()
      .mockResolvedValueOnce(Response.json(session()))
      .mockResolvedValueOnce(Response.json({ error: 'request_forbidden' }, { status: 403 }))
      .mockResolvedValueOnce(Response.json(session()))
      .mockResolvedValueOnce(Response.json(session()))
      .mockResolvedValueOnce(Response.json({ ok: true }))
      .mockResolvedValueOnce(Response.json({ error: 'session_invalid' }, { status: 401 }))
    vi.stubGlobal('fetch', fetchMock)
    setup('/admin')
    await screen.findByText('Estado registrado')
    fireEvent.click(screen.getByRole('button', { name: 'Sair' }))
    expect(await screen.findByRole('alert')).toHaveTextContent('Saída não confirmada')
    expect(screen.getByRole('button', { name: 'Sair' })).toBeInTheDocument()
    fireEvent.click(screen.getByRole('button', { name: 'Tentar sair novamente' }))
    await waitFor(() => expect(fetchMock.mock.calls.filter(([url]) => url === '/api/auth/logout')).toHaveLength(2))
    await screen.findByRole('heading', { name: 'Área administrativa' })
  })

  it('does not let an older session response republish private state after logout', async () => {
    let finishOld!: (response: Response) => void
    const oldResponse = new Promise<Response>(resolve => { finishOld = resolve })
    const fetchMock = vi.fn()
      .mockResolvedValueOnce(Response.json(session()))
      .mockReturnValueOnce(oldResponse)
      .mockResolvedValueOnce(Response.json({ ok: true }))
    vi.stubGlobal('fetch', fetchMock)
    render(<MemoryRouter><AdminAuthProvider monitorSession><AuthProbe /></AdminAuthProvider></MemoryRouter>)
    expect(await screen.findByText('Private probe content')).toBeInTheDocument()
    fireEvent.click(screen.getByRole('button', { name: 'Probe refresh' }))
    await waitFor(() => expect(fetchMock).toHaveBeenCalledTimes(2))
    fireEvent.click(screen.getByRole('button', { name: 'Probe logout' }))
    await waitFor(() => expect(screen.getByText('unauthenticated:none')).toBeInTheDocument())
    finishOld(Response.json(session()))
    await waitFor(() => expect(screen.getByText('unauthenticated:none')).toBeInTheDocument())
    expect(screen.queryByText('Private probe content')).not.toBeInTheDocument()
  })

  it('revalidates an activity CSRF rejection without treating it as revoked membership', async () => {
    const fetchMock = vi.fn()
      .mockResolvedValueOnce(Response.json(session()))
      .mockResolvedValueOnce(Response.json({ error: 'request_forbidden' }, { status: 403 }))
      .mockResolvedValueOnce(Response.json(session()))
    vi.stubGlobal('fetch', fetchMock)
    render(<MemoryRouter><AdminAuthProvider monitorSession><AuthProbe /></AdminAuthProvider></MemoryRouter>)
    expect(await screen.findByText('Private probe content')).toBeInTheDocument()
    fireEvent.pointerDown(window)
    await waitFor(() => expect(fetchMock).toHaveBeenCalledTimes(3))
    expect(await screen.findByText('Private probe content')).toBeInTheDocument()
    expect(await screen.findByText(/authenticated:11111111-/)).toBeInTheDocument()
  })

  it('drops private state on an activity 401 and stops the private view', async () => {
    const fetchMock = vi.fn()
      .mockResolvedValueOnce(Response.json(session()))
      .mockResolvedValueOnce(Response.json({ error: 'session_invalid' }, { status: 401 }))
      .mockResolvedValueOnce(Response.json({ error: 'session_invalid' }, { status: 401 }))
    vi.stubGlobal('fetch', fetchMock)
    setup('/admin')
    expect(await screen.findByText('Estado registrado')).toBeInTheDocument()
    fireEvent.pointerDown(window)
    await screen.findByText('Sua sessão expirou. Entre novamente.')
    expect(screen.queryByText('Estado registrado')).not.toBeInTheDocument()
  })

  it.each([500, 0, 400])('hides protected UI when activity cannot be safely confirmed (%i)', async status => {
    const fetchMock = vi.fn()
      .mockResolvedValueOnce(Response.json(session()))
      .mockImplementationOnce(async () => status === 0
        ? Promise.reject(new Error('network'))
        : Response.json({ error: 'unknown' }, { status }))
    vi.stubGlobal('fetch', fetchMock)
    setup('/admin')
    await screen.findByText('Estado registrado')
    fireEvent.pointerDown(window)
    expect(await screen.findByRole('heading', { name: 'Área administrativa indisponível' })).toBeInTheDocument()
    expect(screen.queryByText('Estado registrado')).not.toBeInTheDocument()
  })

  it('aborts an in-flight activity request when the page becomes hidden', async () => {
    const fetchMock = vi.fn().mockImplementation((input: string, init?: RequestInit) => {
      if (input === '/api/auth/activity') {
        return new Promise((_resolve, reject) => init?.signal?.addEventListener('abort', () => reject(new Error('aborted'))))
      }
      return Promise.resolve(Response.json(session()))
    })
    vi.stubGlobal('fetch', fetchMock)
    render(<MemoryRouter><AdminAuthProvider monitorSession><AuthProbe /></AdminAuthProvider></MemoryRouter>)
    expect(await screen.findByText('Private probe content')).toBeInTheDocument()
    fireEvent.pointerDown(window)
    await waitFor(() => expect(fetchMock).toHaveBeenCalledWith('/api/auth/activity', expect.any(Object)))
    Object.defineProperty(document, 'visibilityState', { configurable: true, value: 'hidden' })
    fireEvent(document, new Event('visibilitychange'))
    await waitFor(() => expect(screen.getByText(/authenticated:11111111-/)).toBeInTheDocument())
    expect(screen.getByText('Private probe content')).toBeInTheDocument()
  })

  it('conceals BFCache-restored private DOM synchronously until pageshow session validation finishes', async () => {
    let finish!: (response: Response) => void
    const pending = new Promise<Response>(resolve => { finish = resolve })
    const fetchMock = vi.fn().mockResolvedValueOnce(Response.json(session())).mockReturnValueOnce(pending)
    vi.stubGlobal('fetch', fetchMock)
    render(<MemoryRouter><AdminAuthProvider monitorSession><AuthProbe /></AdminAuthProvider></MemoryRouter>)
    expect(await screen.findByText('Private probe content')).toBeInTheDocument()
    fireEvent(window, new Event('pageshow'))
    expect(document.documentElement).toHaveAttribute('data-admin-revalidating', 'true')
    expect(screen.queryByText('Private probe content')).not.toBeInTheDocument()
    finish(Response.json({ error: 'session_invalid' }, { status: 401 }))
    await waitFor(() => expect(screen.getByText('unauthenticated:none')).toBeInTheDocument())
    expect(document.documentElement).not.toHaveAttribute('data-admin-revalidating')
  })

  it.each([401, 403] as const)('rejects a late same-user session 200 after private %i authorization loss and fresh revalidation 401, then accepts a later fresh login', async privateStatus => {
    let resolveOld!: (response: Response) => void
    const oldSessionResponse = new Promise<Response>(resolve => { resolveOld = resolve })
    let sessionCalls = 0
    let overviewCalls = 0
    let oldSignal: AbortSignal | undefined
    const oldCsrf = 'a'.repeat(43)
    const freshCsrf = 'f'.repeat(43)
    const refreshResults: AuthSnapshot[] = []
    const fetchMock = vi.fn((input: RequestInfo | URL, init?: RequestInit): Promise<Response> => {
      const url = String(input)
      if (url === '/api/auth/session') {
        sessionCalls += 1
        if (sessionCalls === 1) return Promise.resolve(Response.json(session(oldCsrf)))
        if (sessionCalls === 2) {
          oldSignal = init?.signal ?? undefined
          return oldSessionResponse // Deliberately ignores AbortSignal to prove generation fencing.
        }
        if (sessionCalls === 3) return Promise.resolve(new Response('{}', { status: 401 }))
        if (sessionCalls === 4) return Promise.resolve(Response.json(session(freshCsrf)))
      }
      if (url === '/api/admin/overview') {
        overviewCalls += 1
        if (overviewCalls === 1) return Promise.resolve(new Response('{}', { status: 503 }))
        if (overviewCalls === 2) return Promise.resolve(Response.json({ error: privateStatus === 403 ? 'not_authorized' : 'session_invalid' }, { status: privateStatus }))
        return Promise.resolve(Response.json({}))
      }
      if (url === '/api/admin/providers') return Promise.resolve(new Response('{}', { status: 503 }))
      return Promise.resolve(new Response('{}', { status: 404 }))
    })
    vi.stubGlobal('fetch', fetchMock)
    render(<MemoryRouter initialEntries={['/admin']}><AdminAuthProvider monitorSession><Routes>
      <Route path="/admin/*" element={<><AdminGate /><AuthProbe onRefreshResult={result => refreshResults.push(result)} /></>} />
      <Route path="/login" element={<><LoginPage /><AuthProbe onRefreshResult={result => refreshResults.push(result)} /></>} />
    </Routes></AdminAuthProvider></MemoryRouter>)

    expect(await screen.findByRole('button', { name: 'Reconsultar painel' })).toBeVisible()
    expect(await screen.findByText(new RegExp(`authenticated:${session().user_id}`))).toBeVisible()
    fireEvent.click(screen.getByRole('button', { name: 'Probe refresh' }))
    await waitFor(() => expect(sessionCalls).toBe(2))
    fireEvent.click(screen.getByRole('button', { name: 'Reconsultar painel' }))
    expect(await screen.findByText('Sua sessão expirou. Entre novamente.')).toBeVisible()
    expect(await screen.findByText('unauthenticated:none')).toBeVisible()
    expect(oldSignal?.aborted).toBe(true)
    expect(screen.queryByText('Estado registrado')).not.toBeInTheDocument()

    resolveOld(Response.json(session(oldCsrf)))
    await waitFor(() => expect(refreshResults).toHaveLength(1))
    expect(screen.getByText('unauthenticated:none')).toBeVisible()
    expect(refreshResults[0]).toMatchObject({ status: 'loading', session: null })
    expect(refreshResults[0].session?.csrf_token).not.toBe(oldCsrf)
    expect(overviewCalls).toBe(2)

    fireEvent.click(screen.getByRole('button', { name: 'Probe refresh' }))
    await waitFor(() => expect(sessionCalls).toBe(4))
    expect(refreshResults[1]).toMatchObject({ status: 'authenticated', session: { csrf_token: freshCsrf } })
    expect(await screen.findByText(`authenticated:${session().user_id}`)).toBeVisible()
  })
})
