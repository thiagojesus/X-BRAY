import { useCallback, useEffect, useRef, useState } from 'react'
import { z } from 'zod'
import { type DerivedHistoryResponse, derivedHistoryResponseSchema } from './schema'

class DerivedHistoryHttpError extends Error {
  readonly name = 'DerivedHistoryHttpError'

  constructor(readonly status: number) {
    super(`HTTP ${status}`)
  }
}

type DerivedHistoryState = {
  readonly data: DerivedHistoryResponse | null
  readonly loading: boolean
  readonly refreshing: boolean
  readonly error: string | null
  readonly isStale: boolean
}

export function useDerivedHistory() {
  const [state, setState] = useState<DerivedHistoryState>({
    data: null,
    loading: true,
    refreshing: false,
    error: null,
    isStale: false,
  })
  const controllerRef = useRef<AbortController | null>(null)

  const refetch = useCallback(async () => {
    controllerRef.current?.abort()
    const controller = new AbortController()
    controllerRef.current = controller
    setState(previous => ({ ...previous, loading: previous.data === null, refreshing: true, error: null }))
    try {
      const response = await fetch('/api/derivados/history', { signal: controller.signal })
      if (!response.ok) throw new DerivedHistoryHttpError(response.status)
      const data = derivedHistoryResponseSchema.parse(await response.json())
      if (!controller.signal.aborted) {
        setState({ data, loading: false, refreshing: false, error: null, isStale: false })
      }
    } catch (error: unknown) {
      if (controller.signal.aborted) return
      let message: string
      if (error instanceof DerivedHistoryHttpError) message = `Falha de transporte: HTTP ${error.status}.`
      else if (error instanceof z.ZodError || error instanceof SyntaxError) message = 'Resposta inválida da API; dados rejeitados.'
      else if (error instanceof Error) message = `Falha de transporte: ${error.message}.`
      else throw error
      setState(previous => ({ ...previous, loading: false, refreshing: false, error: message, isStale: previous.data !== null }))
    }
  }, [])

  useEffect(() => {
    void refetch()
    const handleOnline = () => { void refetch() }
    window.addEventListener('online', handleOnline)
    return () => {
      window.removeEventListener('online', handleOnline)
      controllerRef.current?.abort()
    }
  }, [refetch])

  return { ...state, refetch }
}
