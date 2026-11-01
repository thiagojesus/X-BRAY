import { z } from 'zod'

export const sessionSchema = z.object({
  user_id: z.string().uuid(),
  permissions: z.array(z.literal('monitoring:read')).length(1),
  idle_expires_at: z.string().datetime({ offset: true }),
  absolute_expires_at: z.string().datetime({ offset: true }),
  csrf_token: z.string().min(32).max(256),
}).strict().refine(value => Date.parse(value.idle_expires_at) <= Date.parse(value.absolute_expires_at))

export type AdminSession = z.infer<typeof sessionSchema>
export type AuthStatus = 'loading' | 'authenticated' | 'unauthenticated' | 'forbidden' | 'unavailable'
export type AuthSnapshot = { status: AuthStatus; session: AdminSession | null }

export const emptyAuth: AuthSnapshot = { status: 'loading', session: null }

export function authFromSessionResponse(status: number, body: unknown): AuthSnapshot {
  if (status === 401) return { status: 'unauthenticated', session: null }
  if (status === 403) return { status: 'forbidden', session: null }
  if (status !== 200) return { status: 'unavailable', session: null }
  const parsed = sessionSchema.safeParse(body)
  return parsed.success
    ? { status: 'authenticated', session: parsed.data }
    : { status: 'unavailable', session: null }
}

export async function fetchSession(signal?: AbortSignal): Promise<AuthSnapshot> {
  try {
    const response = await fetch('/api/auth/session', {
      method: 'GET', credentials: 'same-origin', cache: 'no-store',
      headers: { Accept: 'application/json' }, signal,
    })
    let body: unknown = null
    if (response.status === 200) {
      try { body = await response.json() } catch { return { status: 'unavailable', session: null } }
    }
    return authFromSessionResponse(response.status, body)
  } catch {
    return { status: 'unavailable', session: null }
  }
}

export async function postAuthAction(
  endpoint: 'activity' | 'logout', csrfToken: string, signal?: AbortSignal,
): Promise<{ status: number; error: string | null }> {
  try {
    const response = await fetch(`/api/auth/${endpoint}`, {
      method: 'POST', credentials: 'same-origin', cache: 'no-store', signal,
      headers: { Accept: 'application/json', 'Content-Type': 'application/json', 'X-CSRF-Token': csrfToken },
      body: '{}',
    })
    let error: string | null = null
    if (response.status >= 400) {
      try {
        const value: unknown = await response.json()
        if (typeof value === 'object' && value !== null && 'error' in value && typeof value.error === 'string') error = value.error
      } catch { /* Treat malformed errors as unknown, never expose response text. */ }
    }
    return { status: response.status, error }
  } catch {
    return { status: 0, error: null }
  }
}

export function nextExpiry(session: AdminSession): number {
  return Math.min(Date.parse(session.idle_expires_at), Date.parse(session.absolute_expires_at))
}
