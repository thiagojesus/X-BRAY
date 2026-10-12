import type { IndicatorUnit, SeriesValue } from '../types/indicators'
import { parseFiniteSeriesValue } from './series'

const ptBr = (value: number, digits: number): string =>
  value.toLocaleString('pt-BR', {
    minimumFractionDigits: digits,
    maximumFractionDigits: digits,
  })

function assertNever(value: never): never {
  throw new RangeError(`Unsupported indicator unit: ${value}`)
}

export function formatIndicatorValue(value: SeriesValue, unit: IndicatorUnit): string | null {
  const parsed = parseFiniteSeriesValue(value)
  if (parsed === null) return null

  switch (unit) {
    case 'pct':
      return `${ptBr(parsed, 1)}%`
    case 'pct_gdp':
      return `${ptBr(parsed, 1)}% do PIB`
    case 'pct_annual':
      return `${ptBr(parsed, 2)}% a.a.`
    case 'index':
      return ptBr(parsed, 1)
    case 'brl':
      return `R$ ${ptBr(parsed, 2)}`
    case 'brl_million':
      return `R$ ${ptBr(parsed, 1)} mi`
    case 'usd_million':
      return `US$ ${ptBr(parsed, 1)} mi`
    case 'ratio':
      return ptBr(parsed, 4)
    default:
      return assertNever(unit)
  }
}
