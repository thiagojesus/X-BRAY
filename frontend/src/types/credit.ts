import { z } from 'zod'

const growthReasonSchema = z.enum([
  'no_valid_observations',
  'invalid_current',
  'missing_current',
  'missing_base',
  'zero_base',
  'invalid_base',
  'invalid_result',
])

const rawPointSchema = z.object({
  data: z.string(),
  valor: z.union([z.string(), z.number(), z.null()]),
  dataFim: z.string().optional(),
})

const metadataSchema = z.object({
  code: z.number(),
  unit: z.enum(['pct', 'pct_gdp', 'pct_annual', 'index', 'brl', 'brl_million', 'usd_million', 'ratio']),
  frequency: z.string(),
  reference_period: z.string().nullable(),
  deadline: z.string().nullable(),
  freshness: z.enum(['ok', 'stale']),
})

const growthMetricSchema = z.object({
  value_pct: z.number().finite().nullable(),
  current_reference: z.string().nullable(),
  base_reference: z.string().nullable(),
  base_observation_reference: z.string().nullable(),
  reason: growthReasonSchema.nullable(),
})

const historyPointSchema = z.object({
  reference: z.string(),
  value: z.number().finite().nullable(),
  mom_pct: z.number().finite().nullable(),
  yoy_pct: z.number().finite().nullable(),
  mom_base_reference: z.string().nullable(),
  yoy_base_reference: z.string().nullable(),
  mom_reason: growthReasonSchema.nullable(),
  yoy_reason: growthReasonSchema.nullable(),
})

export const creditModalitySchema = z.object({
  code: z.number().int().positive(),
  label: z.string(),
  official_label: z.string(),
  short_label: z.string(),
  source_url: z.string().url(),
  frequency: z.literal('monthly'),
  unit: z.literal('brl_million'),
  universe: z.string(),
  measure: z.string(),
  interpretation: z.string(),
  latest: z.object({
    reference: z.string().nullable(),
    value: z.number().finite().nonnegative().nullable(),
    reason: growthReasonSchema.nullable(),
  }),
  mom: growthMetricSchema,
  yoy: growthMetricSchema,
  history: z.array(historyPointSchema),
  unavailable: z.boolean(),
})

const modalitiesSchema = z.object({
  carteira_pf_cheque_especial: creditModalitySchema.nullable().optional(),
  carteira_pf_credito_pessoal_nao_consignado: creditModalitySchema.nullable().optional(),
  carteira_pf_cartao_rotativo: creditModalitySchema.nullable().optional(),
  carteira_pf_cartao_parcelado: creditModalitySchema.nullable().optional(),
}).partial()

const modalityCodes: Readonly<Record<CreditModalityKey, number>> = {
  carteira_pf_cheque_especial: 20573,
  carteira_pf_credito_pessoal_nao_consignado: 20574,
  carteira_pf_cartao_rotativo: 20587,
  carteira_pf_cartao_parcelado: 20588,
}

const baseCreditResponseSchema = z.object({
  last_updated: z.string().nullable().optional(),
  status: z.enum(['ok', 'partial', 'unavailable']),
  data: z.record(z.string(), z.array(rawPointSchema)),
  metadata: z.record(z.string(), metadataSchema),
  unavailable: z.array(z.string()),
})

export type CreditModality = z.infer<typeof creditModalitySchema>
export type CreditModalityKey = keyof z.infer<typeof modalitiesSchema>
export type CreditResponse = z.infer<typeof baseCreditResponseSchema> & {
  readonly modalities?: z.infer<typeof modalitiesSchema>
}

export function parseCreditResponse(input: unknown): CreditResponse | null {
  const parsed = baseCreditResponseSchema.safeParse(input)
  if (!parsed.success) return null
  if (typeof input !== 'object' || input === null || !('modalities' in input)) {
    return parsed.data
  }
  const rawModalities: unknown = input.modalities
  if (typeof rawModalities !== 'object' || rawModalities === null || Array.isArray(rawModalities)) {
    return { ...parsed.data, modalities: {} }
  }
  const valid: Record<string, CreditModality | null> = {}
  for (const [key, value] of Object.entries(rawModalities)) {
    const result = creditModalitySchema.safeParse(value)
    if (result.success && isCreditModalityKey(key) && isExpectedModalitySource(key, result.data)) {
      valid[key] = result.data
    }
    else if (value === null && isCreditModalityKey(key)) valid[key] = null
  }
  return { ...parsed.data, modalities: modalitiesSchema.parse(valid) }
}

function isCreditModalityKey(value: string): value is CreditModalityKey {
  return value === 'carteira_pf_cheque_especial'
    || value === 'carteira_pf_credito_pessoal_nao_consignado'
    || value === 'carteira_pf_cartao_rotativo'
    || value === 'carteira_pf_cartao_parcelado'
}

function isExpectedModalitySource(key: CreditModalityKey, modality: CreditModality): boolean {
  const expectedCode = modalityCodes[key]
  if (modality.code !== expectedCode) return false
  try {
    const source = new URL(modality.source_url)
    return source.protocol === 'https:'
      && source.hostname === 'api.bcb.gov.br'
      && source.pathname === `/dados/serie/bcdata.sgs.${expectedCode}/dados`
      && source.username === ''
      && source.password === ''
      && source.hash === ''
  } catch {
    return false
  }
}
