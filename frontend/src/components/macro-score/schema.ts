import { z } from 'zod'

export const DIMENSION_KEYS = ['activity', 'inflation', 'monetary', 'fiscal', 'credit', 'external'] as const
export const dimensionKeySchema = z.enum(DIMENSION_KEYS)
export const historySeriesSchema = z.enum(['overall', ...DIMENSION_KEYS])
const COMPONENTS_BY_DIMENSION = {
  activity: ['ibc_br_sa_yoy', 'desemprego', 'rendimento_real_yoy'],
  inflation: ['abs_ipca_12m_target', 'abs_focus_12m_target'],
  monetary: ['juro_real_ex_ante_1y', 'inclinacao_di_5y_1y'],
  fiscal: ['saldo_primario_12m_pib', 'dbgg_pib', 'dbgg_yoy_pp', 'saldo_nominal_12m_pib'],
  credit: ['inadimplencia_pf', 'comprometimento_renda', 'icc_pf', 'spread_credito_pf_selic'],
  external: ['cobertura_externa', 'conta_corrente_12m_pib', 'reservas_internacionais'],
} as const satisfies Readonly<Record<DimensionKey, readonly string[]>>
const COMPONENT_KEYS = Object.values(COMPONENTS_BY_DIMENSION).flat()
const monthSchema = z.string().regex(/^\d{4}-(0[1-9]|1[0-2])$/)
const timestampSchema = z.iso.datetime({ offset: true })
const scoreSchema = z.number().min(0).max(100).nullable()
const coverageSchema = z.number().min(0).max(1)
const metadataSchema = z.record(z.string(), z.union([z.string(), z.number(), z.boolean(), z.null()])).readonly()

const inputSchema = z.object({
  value: z.number(),
  unit: z.string().min(1),
  reference_period: z.string().min(1),
  available_at: timestampSchema,
  source: z.string().min(1),
  metadata: metadataSchema,
}).readonly()

const componentSchema = z.object({
  key: z.string().min(1),
  dimension: dimensionKeySchema,
  value: z.number().nullable(),
  unit: z.enum(['pct', 'pct_annual', 'pct_gdp', 'pp', 'ratio', 'usd_million']),
  reference_period: z.iso.date().nullable(),
  available_at: timestampSchema.nullable(),
  methodology_version: z.string().min(1),
  formula: z.string().min(1),
  direction: z.enum(['higher_is_better', 'lower_is_better']),
  weight: z.number().int().positive(),
  percentile: scoreSchema,
  score: scoreSchema,
  reason: z.string().nullable(),
  history_count: z.number().int().nonnegative(),
  history_fingerprint: z.string().min(1),
  inputs: z.record(z.string(), inputSchema).readonly(),
}).readonly()

const dimensionSchema = z.object({
  score: scoreSchema,
  valid_components: z.number().int().nonnegative(),
  total_components: z.number().int().positive(),
  coverage: coverageSchema,
  reason: z.string().nullable(),
  components: z.array(z.string()).readonly(),
}).readonly()

export const macroScoreSchema = z.object({
  score: scoreSchema,
  classification: z.enum(['muito_fraco', 'fraco', 'neutro', 'forte', 'muito_forte']).nullable(),
  calculated_at: timestampSchema,
  as_of: timestampSchema,
  score_month: monthSchema,
  methodology_version: z.literal('0.1.0-beta'),
  overall_coverage: coverageSchema,
  valid_dimensions: z.number().int().min(0).max(6),
  total_dimensions: z.literal(6),
  dimensions: z.record(dimensionKeySchema, dimensionSchema).readonly(),
  components: z.record(z.string(), componentSchema).readonly(),
  warnings: z.array(z.string()).readonly(),
  inputs_hash: z.string().min(1),
  calculation_cycle_id: z.string().min(1),
  origin: z.enum(['production_refresh', 'test_fixture']),
}).refine(snapshot => {
  if (Object.keys(snapshot.components).length !== COMPONENT_KEYS.length) return false
  return DIMENSION_KEYS.every(key => {
    const dimension = snapshot.dimensions[key]
    const expected = COMPONENTS_BY_DIMENSION[key]
    const componentKeys = new Set(dimension.components)
    return dimension.total_components === expected.length
      && dimension.components.length === expected.length
      && expected.every(componentKey => {
        const component = snapshot.components[componentKey]
        return componentKeys.has(componentKey)
          && component?.key === componentKey
          && component.dimension === key
      })
  })
}, { message: 'Invalid dimension/component inventory' }).readonly()

export const macroScoreResponseSchema = z.object({
  status: z.enum(['ok', 'unavailable']),
  data: macroScoreSchema.nullable(),
}).refine(response => response.status === 'ok'
  ? response.data !== null && response.data.score !== null && response.data.classification !== null
  : response.data === null || response.data.score === null,
{ message: 'Invalid score readiness envelope' }).readonly()

export const macroScoreHistorySchema = z.object({
  status: z.enum(['ok', 'unavailable']),
  data: z.array(macroScoreSchema).readonly(),
}).refine(response => response.status === 'ok' ? response.data.length > 0 : response.data.length === 0,
{ message: 'Invalid history envelope' }).readonly()

export type DimensionKey = z.infer<typeof dimensionKeySchema>
export type HistorySeries = z.infer<typeof historySeriesSchema>
export type MacroScorePayload = z.infer<typeof macroScoreSchema>
export type MacroScoreResponse = z.infer<typeof macroScoreResponseSchema>
export type MacroScoreComponent = z.infer<typeof componentSchema>
export type MacroScoreInput = z.infer<typeof inputSchema>
