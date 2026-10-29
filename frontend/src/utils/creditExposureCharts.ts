import type { CreditModality, CreditModalityKey } from '../types/credit'

export type CreditExposureChartRow = {
  readonly date: string
  readonly [key: string]: string | number | null
}

type MutableCreditExposureChartRow = {
  date: string
  [key: string]: string | number | null
}

const EXPOSURE_KEYS: readonly CreditModalityKey[] = [
  'carteira_pf_cheque_especial',
  'carteira_pf_credito_pessoal_nao_consignado',
  'carteira_pf_cartao_rotativo',
  'carteira_pf_cartao_parcelado',
]

type ChartMeasure = 'value' | 'yoy_pct'

function emptyRow(reference: string): MutableCreditExposureChartRow {
  const row: MutableCreditExposureChartRow = { date: reference }
  for (const key of EXPOSURE_KEYS) {
    row[key] = null
    row[`${key}ReferenceDate`] = null
  }
  return row
}

export function mergeCreditExposureHistory(
  modalities: Partial<Record<CreditModalityKey, CreditModality | null>> | undefined,
  measure: ChartMeasure,
): CreditExposureChartRow[] {
  const rows = new Map<string, MutableCreditExposureChartRow>()
  for (const key of EXPOSURE_KEYS) {
    const history = modalities?.[key]?.history
    for (const point of history ?? []) {
      const row = rows.get(point.reference) ?? emptyRow(point.reference)
      row[key] = point[measure]
      row[`${key}ReferenceDate`] = point.reference
      rows.set(point.reference, row)
    }
  }
  return [...rows.entries()]
    .sort(([left], [right]) => left.localeCompare(right))
    .map(([, row]) => row)
}
