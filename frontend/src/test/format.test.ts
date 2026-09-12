import { describe, expect, it } from 'vitest'
import { formatIndicatorValue } from '../utils/format'

describe('formats and merges valid series', () => {
  it.each([
    ['pct', -1.5, '-1,5%'],
    ['pct_gdp', 0, '0,0% do PIB'],
    ['pct_annual', 13.75, '13,75% a.a.'],
    ['index', 153.42, '153,4'],
    ['brl', 1234567.89, 'R$ 1.234.567,89'],
    ['brl_million', '1234567,89', 'R$ 1.234.567,9 mi'],
    ['usd_million', 1234567.89, 'US$ 1.234.567,9 mi'],
    ['ratio', 5.4321, '5,4321'],
  ] as const)('formats %s values by their pt-BR unit', (unit, value, expected) => {
    expect(formatIndicatorValue(value, unit)).toBe(expected)
  })
})

describe('rejects invalid series values', () => {
  it.each([null, undefined, '', 'abc', Number.NaN, Number.POSITIVE_INFINITY])(
    'returns unavailable for %s instead of zero or NaN',
    value => {
      expect(formatIndicatorValue(value, 'index')).toBeNull()
    },
  )
})
