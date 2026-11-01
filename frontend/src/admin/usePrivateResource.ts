import { useCallback, useEffect, useRef, useState } from 'react'
import { useAdminAuth } from './AuthProvider'
import { getPrivate, PrivateApiError } from './adminApi'
import type { ZodType } from 'zod'

type ResourceError = 'invalid' | 'unavailable' | 'notfound'
type State<T> = { key: string; data: T | null; error: ResourceError | null; loading: boolean }

export function usePrivateResource<T>(path: string, schema: ZodType<T>) {
  const { privateEpoch, revalidatePrivateAccess } = useAdminAuth()
  const [version, setVersion] = useState(0)
  const [state, setState] = useState<State<T>>({ key: '', data: null, error: null, loading: true })
  const generation = useRef(0)
  const scopeKey = `${privateEpoch}:${path}:${version}`

  const reload = useCallback(() => setVersion(value => value + 1), [])

  useEffect(() => {
    const current = ++generation.current
    const controller = new AbortController()
    let disposed = false
    let pending = false
    const active = () => !disposed && current === generation.current
    const load = async () => {
      if (pending) return
      pending = true
      setState(previous => ({ key: scopeKey, data: previous.key === scopeKey ? previous.data : null, error: null, loading: true }))
      try {
        const data = await getPrivate(path, schema, controller.signal)
        if (active()) setState({ key: scopeKey, data, error: null, loading: false })
      } catch (error) {
        if (!active() || controller.signal.aborted) return
        if (error instanceof PrivateApiError && (error.kind === 'unauthorized' || error.kind === 'forbidden')) {
          setState({ key: scopeKey, data: null, error: null, loading: true })
          revalidatePrivateAccess()
        } else {
          const kind: ResourceError = error instanceof PrivateApiError && error.kind === 'invalid' ? 'invalid' : error instanceof PrivateApiError && error.kind === 'notfound' ? 'notfound' : 'unavailable'
          setState({ key: scopeKey, data: null, error: kind, loading: false })
        }
      } finally {
        pending = false
      }
    }
    void load()
    const timer = window.setInterval(() => {
      if (document.visibilityState === 'visible' && navigator.onLine && !pending && active()) void load()
    }, 60_000)
    return () => {
      disposed = true
      generation.current += 1
      controller.abort()
      window.clearInterval(timer)
    }
  }, [path, revalidatePrivateAccess, schema, scopeKey])

  return { ...(state.key === scopeKey ? state : { data: null, error: null, loading: true }), reload }
}
