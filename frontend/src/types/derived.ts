export const ADDITIONAL_INDICATORS = [
  'cobertura_externa',
  'variacao_saldo_primario_12m',
  'spread_credito_pf_selic',
  'distancia_focus_meta',
  'inclinacao_di_5y_1y',
] as const

export type AdditionalIndicatorKey = (typeof ADDITIONAL_INDICATORS)[number]

export type DerivedMetadataValue = string | number | boolean | null

export type DerivedInput = {
  readonly value: number
  readonly unit: string
  readonly reference_period: string
  readonly available_at: string
  readonly source: string
  readonly metadata?: Readonly<Record<string, DerivedMetadataValue>>
}

type SnapshotFields = {
  readonly key: AdditionalIndicatorKey
  readonly unit: 'ratio' | 'pp'
  readonly as_of: string
  readonly methodology_version: string
  readonly formula: string
  readonly reference_date: string | null
  readonly inputs: Readonly<Record<string, DerivedInput>>
}

export type AdditionalIndicatorSnapshot =
  | (SnapshotFields & { readonly kind: 'available'; readonly value: number })
  | (SnapshotFields & { readonly kind: 'unavailable'; readonly value: null; readonly reason: string })
  | { readonly kind: 'invalid'; readonly key: AdditionalIndicatorKey }

export type AdditionalIndicatorResponse =
  | { readonly kind: 'empty' }
  | { readonly kind: 'invalid_envelope' }
  | {
      readonly kind: 'populated'
      readonly snapshots: Readonly<Partial<Record<AdditionalIndicatorKey, AdditionalIndicatorSnapshot>>>
    }

const EXPECTED_UNITS = {
  cobertura_externa: 'ratio',
  variacao_saldo_primario_12m: 'pp',
  spread_credito_pf_selic: 'pp',
  distancia_focus_meta: 'pp',
  inclinacao_di_5y_1y: 'pp',
} as const satisfies Readonly<Record<AdditionalIndicatorKey, 'ratio' | 'pp'>>

function isRecord(value: unknown): value is Record<string, unknown> {
  return typeof value === 'object' && value !== null && !Array.isArray(value)
}

function isFiniteNumber(value: unknown): value is number {
  return typeof value === 'number' && Number.isFinite(value)
}

function isTimestamp(value: unknown): value is string {
  return typeof value === 'string'
    && /(?:Z|[+-]\d{2}:\d{2})$/.test(value)
    && Number.isFinite(Date.parse(value))
}

function isDate(value: unknown): value is string {
  if (typeof value !== 'string' || !/^\d{4}-\d{2}-\d{2}$/.test(value)) return false
  const parsed = new Date(`${value}T00:00:00.000Z`)
  return Number.isFinite(parsed.getTime()) && parsed.toISOString().slice(0, 10) === value
}

function isMetadataValue(value: unknown): value is DerivedMetadataValue {
  return value === null || typeof value === 'string' || typeof value === 'boolean' || isFiniteNumber(value)
}

function parseMetadata(value: unknown): Readonly<Record<string, DerivedMetadataValue>> | null {
  if (!isRecord(value)) return null
  const metadata: Record<string, DerivedMetadataValue> = {}
  for (const [key, metadataValue] of Object.entries(value)) {
    if (!isMetadataValue(metadataValue)) return null
    metadata[key] = metadataValue
  }
  return metadata
}

function parseInput(value: unknown, asOf: string): DerivedInput | null {
  if (!isRecord(value)
    || !isFiniteNumber(value.value)
    || typeof value.unit !== 'string'
    || !isDate(value.reference_period)
    || !isTimestamp(value.available_at)
    || Date.parse(value.available_at) > Date.parse(asOf)
    || typeof value.source !== 'string'
    || value.source.length === 0) return null

  if (value.metadata !== undefined) {
    const metadata = parseMetadata(value.metadata)
    if (metadata === null) return null
    return {
      value: value.value,
      unit: value.unit,
      reference_period: value.reference_period,
      available_at: value.available_at,
      source: value.source,
      metadata,
    }
  }

  return {
    value: value.value,
    unit: value.unit,
    reference_period: value.reference_period,
    available_at: value.available_at,
    source: value.source,
  }
}

function parseSnapshot(key: AdditionalIndicatorKey, value: unknown): AdditionalIndicatorSnapshot {
  if (!isRecord(value)
    || value.key !== key
    || value.unit !== EXPECTED_UNITS[key]
    || !isTimestamp(value.as_of)
    || typeof value.methodology_version !== 'string'
    || value.methodology_version.length === 0
    || typeof value.formula !== 'string'
    || value.formula.length === 0
    || !isRecord(value.inputs)) return { kind: 'invalid', key }

  const referenceDate = value.reference_date
  if (referenceDate !== undefined && referenceDate !== null && !isDate(referenceDate)) {
    return { kind: 'invalid', key }
  }

  const inputs: Record<string, DerivedInput> = {}
  for (const [inputKey, rawInput] of Object.entries(value.inputs)) {
    const input = parseInput(rawInput, value.as_of)
    if (input === null) return { kind: 'invalid', key }
    inputs[inputKey] = input
  }

  const fields: SnapshotFields = {
    key,
    unit: EXPECTED_UNITS[key],
    as_of: value.as_of,
    methodology_version: value.methodology_version,
    formula: value.formula,
    reference_date: typeof referenceDate === 'string' ? referenceDate : null,
    inputs,
  }

  if (value.value === null && typeof value.reason === 'string' && value.reason.length > 0) {
    return { ...fields, kind: 'unavailable', value: null, reason: value.reason }
  }
  if (isFiniteNumber(value.value) && value.reason === null) {
    return { ...fields, kind: 'available', value: value.value }
  }
  return { kind: 'invalid', key }
}

export function parseAdditionalIndicatorResponse(value: unknown): AdditionalIndicatorResponse {
  if (!isRecord(value)
    || (value.status !== 'ok' && value.status !== 'unavailable')
    || !isRecord(value.data)) return { kind: 'invalid_envelope' }

  const keys = Object.keys(value.data)
  if (value.status === 'unavailable' && keys.length === 0) return { kind: 'empty' }
  if (value.status !== 'ok' || keys.length === 0) return { kind: 'invalid_envelope' }

  const snapshots: Partial<Record<AdditionalIndicatorKey, AdditionalIndicatorSnapshot>> = {}
  for (const key of ADDITIONAL_INDICATORS) {
    const rawSnapshot = value.data[key]
    if (rawSnapshot !== undefined) snapshots[key] = parseSnapshot(key, rawSnapshot)
  }
  return { kind: 'populated', snapshots }
}
