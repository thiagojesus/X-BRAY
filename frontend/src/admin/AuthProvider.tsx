import { createContext, useCallback, useContext, useEffect, useMemo, useRef, useState } from 'react'
import { useLocation, useNavigate } from 'react-router-dom'
import {
  emptyAuth, fetchSession, nextExpiry, postAuthAction,
  type AuthSnapshot,
} from './authClient'

type AuthContextValue = {
  auth: AuthSnapshot
  privateEpoch: number
  revalidatePrivateAccess: () => void
  logoutError: boolean
  refresh: (conceal?: boolean) => Promise<AuthSnapshot>
  logout: () => Promise<void>
  retryLogout: () => Promise<void>
}

const AuthContext = createContext<AuthContextValue | null>(null)

export function useAdminAuth(): AuthContextValue {
  const value = useContext(AuthContext)
  if (!value) throw new Error('useAdminAuth requires AdminAuthProvider')
  return value
}

export function AdminAuthProvider({ children, monitorSession = false }: {
  children: React.ReactNode
  monitorSession?: boolean
}) {
  const [auth, setAuth] = useState<AuthSnapshot>(emptyAuth)
  const [logoutError, setLogoutError] = useState(false)
  const [privateEpoch, setPrivateEpoch] = useState(0)
  const epoch = useRef(0)
  const activeUser = useRef<string | null>(null)
  const authIdentity = useRef<{ userId: string; csrf: string } | null>(null)
  const controllers = useRef(new Set<AbortController>())
  const activityController = useRef<AbortController | null>(null)
  const lastActivity = useRef(0)
  const location = useLocation()
  const navigate = useNavigate()

  const abortOutstanding = useCallback(() => {
    for (const controller of controllers.current) controller.abort()
    controllers.current.clear()
  }, [])

  const invalidatePrivateScope = useCallback(() => {
    authIdentity.current = null
    setPrivateEpoch(value => value + 1)
  }, [])

  const applySnapshot = useCallback((next: AuthSnapshot, requestEpoch: number) => {
    if (requestEpoch !== epoch.current) return
    const previousIdentity = authIdentity.current
    const nextIdentity = next.status === 'authenticated' && next.session
      ? { userId: next.session.user_id, csrf: next.session.csrf_token } : null
    if (previousIdentity?.userId !== nextIdentity?.userId || previousIdentity?.csrf !== nextIdentity?.csrf) {
      authIdentity.current = nextIdentity
      setPrivateEpoch(value => value + 1)
    }
    if (activeUser.current && next.session && activeUser.current !== next.session.user_id) {
      epoch.current += 1
      abortOutstanding()
    }
    activeUser.current = next.session?.user_id ?? null
    setAuth(next)
  }, [abortOutstanding])

  const refresh = useCallback(async (conceal = false): Promise<AuthSnapshot> => {
    const requestEpoch = epoch.current
    if (conceal) setAuth(emptyAuth)
    const controller = new AbortController()
    controllers.current.add(controller)
    let next = await fetchSession(controller.signal)
    controllers.current.delete(controller)
    if (requestEpoch !== epoch.current) return emptyAuth
    if (next.status === 'authenticated' && next.session && nextExpiry(next.session) <= Date.now()) {
      next = { status: 'unavailable', session: null }
    }
    applySnapshot(next, requestEpoch)
    return next
  }, [applySnapshot])

  const revalidatePrivateAccess = useCallback(() => {
    epoch.current += 1
    abortOutstanding()
    invalidatePrivateScope()
    setAuth(emptyAuth)
    void refresh(true)
  }, [abortOutstanding, invalidatePrivateScope, refresh])

  const clearAndNavigate = useCallback((destination = '/login?reason=session_expired') => {
    epoch.current += 1
    invalidatePrivateScope()
    abortOutstanding()
    setAuth({ status: 'unauthenticated', session: null })
    activeUser.current = null
    setLogoutError(false)
    navigate(destination, { replace: true })
  }, [abortOutstanding, invalidatePrivateScope, navigate])

  const doLogout = useCallback(async (sessionCsrf: string) => {
    epoch.current += 1
    invalidatePrivateScope()
    const logoutEpoch = epoch.current
    abortOutstanding()
    setAuth({ status: 'unavailable', session: null })
    activeUser.current = null
    setLogoutError(false)
    const controller = new AbortController()
    controllers.current.add(controller)
    const result = await postAuthAction('logout', sessionCsrf, controller.signal)
    controllers.current.delete(controller)
    if (logoutEpoch !== epoch.current) return
    if (result.status >= 200 && result.status < 300 || result.status === 401 || result.error === 'not_authorized') {
      clearAndNavigate('/login?reason=signed_out')
      return
    }
    if (result.status === 403 && result.error === 'request_forbidden') {
      await refresh(true)
      setLogoutError(true)
      return
    }
    setLogoutError(true)
  }, [abortOutstanding, clearAndNavigate, invalidatePrivateScope, refresh])

  const logout = useCallback(async () => {
    const csrf = auth.session?.csrf_token
    if (!csrf) {
      await refresh(true)
      setLogoutError(true)
      return
    }
    await doLogout(csrf)
  }, [auth.session?.csrf_token, doLogout, refresh])

  const retryLogout = useCallback(async () => {
    const current = await refresh(true)
    if (current.status === 'authenticated' && current.session) {
      await doLogout(current.session.csrf_token)
    } else if (current.status === 'unauthenticated' || current.status === 'forbidden') {
      clearAndNavigate('/login?reason=signed_out')
    } else {
      setLogoutError(true)
    }
  }, [clearAndNavigate, doLogout, refresh])

  useEffect(() => {
    void refresh(true)
    return () => {
      epoch.current += 1
      abortOutstanding()
    }
  }, [abortOutstanding, refresh])

  useEffect(() => {
    if (auth.status !== 'authenticated' || !auth.session) return
    const expireAt = nextExpiry(auth.session)
    const delay = expireAt - Date.now()
    if (delay <= 0) {
      void refresh(true)
      return
    }
    const timer = window.setTimeout(() => { void refresh(true) }, delay)
    return () => window.clearTimeout(timer)
  }, [auth, refresh])

  useEffect(() => {
    if (auth.status !== 'authenticated') return
    const onResume = () => {
      if (document.visibilityState === 'visible') {
        document.documentElement.setAttribute('data-admin-revalidating', 'true')
        void refresh(true)
      }
    }
      document.addEventListener('visibilitychange', onResume)
      window.addEventListener('pageshow', onResume)
      window.addEventListener('focus', onResume)
    return () => {
      document.removeEventListener('visibilitychange', onResume)
      window.removeEventListener('pageshow', onResume)
      window.removeEventListener('focus', onResume)
    }
  }, [auth.status, refresh])

  useEffect(() => {
    if (auth.status !== 'loading') document.documentElement.removeAttribute('data-admin-revalidating')
  }, [auth.status])

  useEffect(() => () => document.documentElement.removeAttribute('data-admin-revalidating'), [])

  useEffect(() => {
    if (!monitorSession || auth.status !== 'authenticated') return
    const interval = window.setInterval(() => {
      if (document.visibilityState === 'visible') void refresh()
    }, 60_000)
    return () => window.clearInterval(interval)
  }, [auth.status, monitorSession, refresh])

  useEffect(() => {
    if (!monitorSession || auth.status !== 'authenticated' || !auth.session) return
    const signalActivity = () => {
      if (document.visibilityState !== 'visible' || Date.now() - lastActivity.current < 60_000) return
      lastActivity.current = Date.now()
      const csrf = auth.session?.csrf_token
      if (!csrf) return
      const controller = new AbortController()
      activityController.current = controller
      controllers.current.add(controller)
      void postAuthAction('activity', csrf, controller.signal).then(async result => {
        controllers.current.delete(controller)
        if (activityController.current === controller) activityController.current = null
        if (controller.signal.aborted) return
        if (result.status === 401 || result.error === 'not_authorized') {
          const next = result.status === 401
            ? { status: 'unauthenticated' as const, session: null }
            : { status: 'forbidden' as const, session: null }
          epoch.current += 1
          invalidatePrivateScope()
          activeUser.current = null
          applySnapshot(next, epoch.current)
          return
        }
        if (result.status === 403 && result.error === 'request_forbidden') {
          const next = await refresh(true)
          if (next.status !== 'authenticated') return
          return
        }
        if (result.status === 0 || result.status >= 500) {
          epoch.current += 1
          invalidatePrivateScope()
          abortOutstanding()
          activeUser.current = null
          setAuth({ status: 'unavailable', session: null })
          return
        }
        if (result.status >= 200 && result.status < 300) await refresh()
        else {
          epoch.current += 1
          invalidatePrivateScope()
          abortOutstanding()
          activeUser.current = null
          setAuth({ status: 'unavailable', session: null })
        }
      })
    }
    const attach = () => {
      window.addEventListener('pointerdown', signalActivity, { passive: true })
      window.addEventListener('keydown', signalActivity)
      window.addEventListener('touchstart', signalActivity, { passive: true })
    }
    const detach = () => {
      window.removeEventListener('pointerdown', signalActivity)
      window.removeEventListener('keydown', signalActivity)
      window.removeEventListener('touchstart', signalActivity)
    }
    const onVisibility = () => {
      if (document.visibilityState === 'hidden') {
        detach()
        activityController.current?.abort()
        if (activityController.current) controllers.current.delete(activityController.current)
        activityController.current = null
      }
    }
    attach()
    document.addEventListener('visibilitychange', onVisibility)
    return () => {
      detach()
      document.removeEventListener('visibilitychange', onVisibility)
    }
  }, [abortOutstanding, applySnapshot, auth, invalidatePrivateScope, monitorSession, refresh])

  useEffect(() => {
    if (auth.status === 'forbidden' && location.pathname.startsWith('/admin')) {
      navigate('/login?reason=access_denied', { replace: true })
    }
  }, [auth.status, location.pathname, navigate])

  const value = useMemo(() => ({ auth, privateEpoch, revalidatePrivateAccess, logoutError, refresh, logout, retryLogout }), [auth, privateEpoch, revalidatePrivateAccess, logoutError, refresh, logout, retryLogout])
  return <AuthContext.Provider value={value}>{children}</AuthContext.Provider>
}
