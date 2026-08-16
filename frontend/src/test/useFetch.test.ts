import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest'
import { renderHook, waitFor } from '@testing-library/react'
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
    result.current.refetch()
    await waitFor(() => expect(result.current.data).toEqual(payload2))
  })
})
