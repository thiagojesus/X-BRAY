import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest'
import { act, renderHook, waitFor } from '@testing-library/react'
import { useFetch } from '../hooks/useFetch'

const mockFetch = vi.fn()

beforeEach(() => {
  vi.stubGlobal('fetch', mockFetch)
})

afterEach(() => {
  vi.restoreAllMocks()
})

describe('useFetch', () => {
  it('starts with loading state', () => {
    mockFetch.mockResolvedValue({ ok: true, json: () => Promise.resolve({}) })
    const { result } = renderHook(() => useFetch('/api/test'))
    expect(result.current.loading).toBe(true)
    expect(result.current.data).toBeNull()
    expect(result.current.error).toBeNull()
  })

  it('fetches data successfully', async () => {
    const payload = { status: 'ok', items: [1, 2, 3] }
    mockFetch.mockResolvedValue({ ok: true, json: () => Promise.resolve(payload) })
    const { result } = renderHook(() => useFetch('/api/test'))
    await waitFor(() => expect(result.current.loading).toBe(false))
    expect(result.current.data).toEqual(payload)
    expect(result.current.error).toBeNull()
    expect(result.current.isStale).toBe(false)
    expect(result.current.lastSuccessfulAt).not.toBeNull()
  })

  it('handles HTTP error', async () => {
    mockFetch.mockResolvedValue({ ok: false, status: 500 })
    const { result } = renderHook(() => useFetch('/api/test'))
    await waitFor(() => expect(result.current.loading).toBe(false))
    expect(result.current.error).toBe('HTTP 500')
    expect(result.current.data).toBeNull()
  })

  it('handles network error', async () => {
    mockFetch.mockRejectedValue(new Error('Network fail'))
    const { result } = renderHook(() => useFetch('/api/test'))
    await waitFor(() => expect(result.current.loading).toBe(false))
    expect(result.current.error).toBe('Network fail')
    expect(result.current.data).toBeNull()
  })

  it('refetch reloads data', async () => {
    const payload = { v: 1 }
    mockFetch.mockResolvedValue({ ok: true, json: () => Promise.resolve(payload) })
    const { result } = renderHook(() => useFetch('/api/test'))
    await waitFor(() => expect(result.current.loading).toBe(false))
    expect(result.current.data).toEqual(payload)
    const payload2 = { v: 2 }
    mockFetch.mockResolvedValue({ ok: true, json: () => Promise.resolve(payload2) })
    await act(async () => { await result.current.refetch() })
    await waitFor(() => expect(result.current.data).toEqual(payload2))
  })

  it('keeps the last good payload and marks it stale when a refetch fails', async () => {
    mockFetch.mockResolvedValueOnce({ ok: true, json: () => Promise.resolve({ v: 1 }) })
    const { result } = renderHook(() => useFetch<{ v: number }>('/api/test'))
    await waitFor(() => expect(result.current.data).toEqual({ v: 1 }))

    mockFetch.mockRejectedValueOnce(new Error('offline'))
    await act(async () => { await result.current.refetch() })

    await waitFor(() => expect(result.current.error).toBe('offline'))
    expect(result.current.data).toEqual({ v: 1 })
    expect(result.current.isStale).toBe(true)
    expect(result.current.loading).toBe(false)
  })

  it('clears stale metadata after a successful retry', async () => {
    mockFetch
      .mockResolvedValueOnce({ ok: true, json: () => Promise.resolve({ v: 1 }) })
      .mockRejectedValueOnce(new Error('offline'))
      .mockResolvedValueOnce({ ok: true, json: () => Promise.resolve({ v: 2 }) })
    const { result } = renderHook(() => useFetch<{ v: number }>('/api/test'))
    await waitFor(() => expect(result.current.data).toEqual({ v: 1 }))
    await act(async () => { await result.current.refetch() })
    await waitFor(() => expect(result.current.isStale).toBe(true))
    await act(async () => { await result.current.refetch() })
    await waitFor(() => expect(result.current.data).toEqual({ v: 2 }))
    expect(result.current.error).toBeNull()
    expect(result.current.isStale).toBe(false)
  })

  it('aborts an in-flight request on unmount', () => {
    mockFetch.mockImplementation((_url: string, init: RequestInit) => {
      expect(init.signal).toBeInstanceOf(AbortSignal)
      return new Promise(() => {})
    })
    const { unmount } = renderHook(() => useFetch('/api/test'))
    const signal = mockFetch.mock.calls[0]?.[1]?.signal as AbortSignal
    unmount()
    expect(signal.aborted).toBe(true)
  })

  it('retries on the browser online event without discarding last good data', async () => {
    mockFetch.mockResolvedValueOnce({ ok: true, json: () => Promise.resolve({ v: 1 }) })
    const { result } = renderHook(() => useFetch<{ v: number }>('/api/test'))
    await waitFor(() => expect(result.current.data).toEqual({ v: 1 }))
    mockFetch.mockRejectedValueOnce(new Error('offline'))

    act(() => window.dispatchEvent(new Event('online')))

    await waitFor(() => expect(result.current.isStale).toBe(true))
    expect(result.current.data).toEqual({ v: 1 })
  })
})
