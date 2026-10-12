import { describe, expect, it } from 'vitest'
import {
  groupByMonthLastValue,
  groupMonthlyLast,
  lastValidPoint,
  mergeSeriesByMonthLastValue,
  mergeSeriesByDate,
  normalizeSeriesBase100,
  parseFiniteSeriesValue,
  parseSeriesDate,
} from '../utils/series'
import type { RawSeriesPoint } from '../types/indicators'

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

  it('aligns monthly series by their last observation and preserves explicit gaps', () => {
    const result = mergeSeriesByMonthLastValue({
      selic_meta: [
        { data: '01/03/2026', valor: '14.00' },
        { data: '29/03/2026', valor: '14.25' },
      ],
      taxa_media_credito_pf: [
        { data: '01/02/2026', valor: '14.40' },
        { data: '01/04/2026', valor: '14.20' },
      ],
    })

    expect(groupByMonthLastValue([
      { data: '01/03/2026', valor: '14.00' },
      { data: '29/03/2026', valor: '14.25' },
    ])).toEqual([{ date: '29/03/2026', value: 14.25 }])
    expect(result).toEqual([
      {
        date: '01/02/2026',
        selic_meta: null,
        selic_metaReferenceDate: null,
        taxa_media_credito_pf: 14.4,
        taxa_media_credito_pfReferenceDate: '01/02/2026',
      },
      {
        date: '01/03/2026',
        selic_meta: 14.25,
        selic_metaReferenceDate: '29/03/2026',
        taxa_media_credito_pf: null,
        taxa_media_credito_pfReferenceDate: null,
      },
      {
        date: '01/04/2026',
        selic_meta: null,
        selic_metaReferenceDate: null,
        taxa_media_credito_pf: 14.2,
        taxa_media_credito_pfReferenceDate: '01/04/2026',
      },
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

  it('ignores malformed members received at the JSON boundary', () => {
    const points = [
      null,
      42,
      'invalid',
      { data: '01/02/2026' },
      { valor: '2' },
      { data: '02/02/2026', valor: '2' },
    ] as unknown as readonly RawSeriesPoint[]

    expect(lastValidPoint(points)).toEqual({ date: '02/02/2026', value: 2 })
    expect(groupByMonthLastValue(points)).toEqual([{ date: '02/02/2026', value: 2 }])
    expect(mergeSeriesByDate({ first: points })).toEqual([{ date: '02/02/2026', first: 2 }])
    expect(mergeSeriesByMonthLastValue({ first: points })).toEqual([{
      date: '01/02/2026',
      first: 2,
      firstReferenceDate: '02/02/2026',
    }])
  })
})

describe('normalizes index series after the selected window', () => {
  it('uses the first finite nonzero value inside the supplied window as base 100', () => {
    const result = normalizeSeriesBase100(
      [
        { date: '01/01/2025', total: 110, agro: 0 },
        { date: '01/02/2025', total: 121, agro: 50 },
      ],
      ['total', 'agro'],
    )

    expect(result).toEqual({
      data: [
        { date: '01/01/2025', total: 100, agro: 0 },
        { date: '01/02/2025', total: 110, agro: 100 },
      ],
      unavailable: [],
    })
  })

  it('marks a series unavailable when the selected window has no finite nonzero base', () => {
    const result = normalizeSeriesBase100(
      [
        { date: '01/01/2025', total: 110, services: 0 },
        { date: '01/02/2025', total: 121, services: 0 },
      ],
      ['total', 'services'],
    )

    expect(result).toEqual({
      data: [
        { date: '01/01/2025', total: 100 },
        { date: '01/02/2025', total: 110 },
      ],
      unavailable: ['services'],
    })
  })
})
