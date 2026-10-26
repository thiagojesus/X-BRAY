export const INDICATOR_UNITS = [
  'pct',
  'pct_gdp',
  'pct_annual',
  'index',
  'brl',
  'brl_million',
  'usd_million',
  'ratio',
] as const

export type IndicatorUnit = (typeof INDICATOR_UNITS)[number]

export type SeriesValue = string | number | null | undefined

export type RawSeriesPoint = {
  readonly data: string
  readonly valor: SeriesValue
  readonly dataFim?: string
}

export type ParsedSeriesPoint = {
  readonly date: string
  readonly value: number
}

export type ValidSeriesPoint = {
  readonly data: string
  readonly valor: string | number
  readonly value: number
}

export type SeriesRow = {
  readonly [key: string]: string | number
  readonly date: string
}

export type SeriesMap = Readonly<Record<string, readonly RawSeriesPoint[] | undefined>>
export type SeriesTransform = (value: number) => number
export type SeriesTransforms = Readonly<Record<string, SeriesTransform | undefined>>
