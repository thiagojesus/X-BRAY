import { describe, expect, it } from 'vitest'
import {
  groupMonthlyLast,
  lastValidPoint,
  mergeSeriesByDate,
  parseFiniteSeriesValue,
  parseSeriesDate,
} from '../utils/series'

describe('formats and merges valid series', () => {
  it('merges sparse series by chronological date without forward filling', () => {
    const result = mergeSeriesByDate({
      first: [
        { data: '02/01/2026', valor: '2,5' },
        { data: '01/01/2026', valor: '-1.25' },
      ],
      second: [
        { data: '01/01/2026', valor: 0 },
        { data: '03/01/2026', valor: '1000000' },
      ],
    })

    expect(result).toEqual([
      { date: '01/01/2026', first: -1.25, second: 0 },
      { date: '02/01/2026', first: 2.5 },
      { date: '03/01/2026', second: 1_000_000 },
    ])
  })

  it('returns the chronologically latest finite observation', () => {
    const result = lastValidPoint([
      { data: '03/01/2026', valor: 'abc' },
      { data: '01/01/2026', valor: '1' },
      { data: '02/01/2026', valor: '2,75' },
    ])

    expect(result).toEqual({ date: '02/01/2026', value: 2.75 })
  })

  it('groups each month by its last observed finite value', () => {
    const result = groupMonthlyLast([
      { data: '15/02/2026', valor: '4' },
      { data: '31/01/2026', valor: '3' },
      { data: '01/02/2026', valor: '2' },
      { data: '01/01/2026', valor: '1' },
    ])

    expect(result).toEqual([
      { date: '31/01/2026', value: 3 },
      { date: '15/02/2026', value: 4 },
    ])
  })
})

describe('rejects invalid series values', () => {
  it.each(['31/02/2026', '2026-02-28', '', '1/02/2026'])('rejects invalid date %s', value => {
    expect(parseSeriesDate(value)).toBeNull()
  })

  it.each(['abc', '', ' ', '1,2,3', 'Infinity'])('rejects invalid numeric text %s', value => {
    expect(parseFiniteSeriesValue(value)).toBeNull()
  })

  it.each([null, undefined, Number.NaN, Number.POSITIVE_INFINITY, Number.NEGATIVE_INFINITY])(
    'rejects invalid numeric value %s',
    value => {
      expect(parseFiniteSeriesValue(value)).toBeNull()
    },
  )

  it('omits invalid values and keeps unaligned dates sparse', () => {
    const result = mergeSeriesByDate({
      first: [
        { data: '01/01/2026', valor: 'abc' },
        { data: '02/01/2026', valor: '2' },
      ],
      second: [{ data: '03/01/2026', valor: '3' }],
    })

    expect(result).toEqual([
      { date: '02/01/2026', first: 2 },
      { date: '03/01/2026', second: 3 },
    ])
  })
})
