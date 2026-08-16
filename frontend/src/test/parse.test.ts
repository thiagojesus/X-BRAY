import { describe, it, expect } from 'vitest'
import { parseSgsData, formatDate, shortDate } from '../utils/parse'

describe('parseSgsData', () => {
  it('parses comma decimal and sorts by date', () => {
    const raw = [
      { data: '01/03/2025', valor: '12,5' },
      { data: '01/01/2025', valor: '10,0' },
      { data: '01/02/2025', valor: '11,3' },
    ]
    const result = parseSgsData(raw)
    expect(result).toEqual([
      { date: '01/01/2025', value: 10 },
      { date: '01/02/2025', value: 11.3 },
      { date: '01/03/2025', value: 12.5 },
    ])
  })

  it('filters out NaN values', () => {
    const raw = [
      { data: '01/01/2025', valor: '10,0' },
      { data: '01/02/2025', valor: 'abc' },
    ]
    const result = parseSgsData(raw)
    expect(result).toHaveLength(1)
    expect(result[0].value).toBe(10)
  })

  it('handles dot decimal', () => {
    const raw = [{ data: '15/06/2024', valor: '3.14' }]
    const result = parseSgsData(raw)
    expect(result[0].value).toBe(3.14)
  })

  it('returns empty for empty input', () => {
    expect(parseSgsData([])).toEqual([])
  })
})

describe('formatDate', () => {
  it('converts DD/MM/YYYY to YYYY-MM-DD', () => {
    expect(formatDate('01/03/2025')).toBe('2025-03-01')
    expect(formatDate('25/12/2024')).toBe('2024-12-25')
  })
})

describe('shortDate', () => {
  it('returns MM/YY format', () => {
    expect(shortDate('01/03/2025')).toBe('03/25')
    expect(shortDate('25/12/2024')).toBe('12/24')
  })
})
