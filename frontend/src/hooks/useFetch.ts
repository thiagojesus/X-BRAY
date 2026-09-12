import { useState, useEffect, useCallback, useRef } from 'react'

interface FetchState<T> {
  data: T | null
  loading: boolean
  error: string | null
  isStale: boolean
  lastSuccessfulAt: string | null
}

interface FetchResult<T> extends FetchState<T> {
  refetch: () => Promise<void>
}

function errorMessage(error: unknown): string {
  return error instanceof Error ? error.message : 'Falha desconhecida'
}

export function useFetch<T>(url: string): FetchResult<T> {
  const [state, setState] = useState<FetchState<T>>({
    data: null,
    loading: Boolean(url),
    error: null,
    isStale: false,
    lastSuccessfulAt: null,
  })
  const controllerRef = useRef<AbortController | null>(null)
  const previousUrlRef = useRef(url)

  const fetchData = useCallback(async () => {
    controllerRef.current?.abort()
    if (!url) {
      setState({ data: null, loading: false, error: null, isStale: false, lastSuccessfulAt: null })
      return
    }
    const controller = new AbortController()
    controllerRef.current = controller
    setState(prev => ({ ...prev, loading: prev.data === null, error: null }))
    try {
      const resp = await fetch(url, { signal: controller.signal })
      if (!resp.ok) throw new Error(`HTTP ${resp.status}`)
      const json: T = await resp.json()
      if (controller.signal.aborted) return
      setState({
        data: json,
        loading: false,
        error: null,
        isStale: false,
        lastSuccessfulAt: new Date().toISOString(),
      })
    } catch (error: unknown) {
      if (controller.signal.aborted) return
      setState(prev => ({
        ...prev,
        loading: false,
        error: errorMessage(error),
        isStale: prev.data !== null,
      }))
    }
  }, [url])

  useEffect(() => {
    if (previousUrlRef.current !== url) {
      previousUrlRef.current = url
      setState({ data: null, loading: Boolean(url), error: null, isStale: false, lastSuccessfulAt: null })
    }
    void fetchData()
    const handleOnline = () => { void fetchData() }
    window.addEventListener('online', handleOnline)
    return () => {
      window.removeEventListener('online', handleOnline)
      controllerRef.current?.abort()
    }
  }, [fetchData, url])

  return { ...state, refetch: fetchData }
}
