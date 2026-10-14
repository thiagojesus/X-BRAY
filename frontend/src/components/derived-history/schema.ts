import { z } from 'zod'

export const DERIVED_HISTORY_KEYS = [
  'juro_real_ex_ante_1y',
  'cobertura_externa',
  'variacao_saldo_primario_12m',
  'spread_credito_pf_selic',
  'distancia_focus_meta',
  'inclinacao_di_5y_1y',
] as const

export const derivedHistoryKeySchema = z.enum(DERIVED_HISTORY_KEYS)
export type DerivedHistoryKey = z.infer<typeof derivedHistoryKeySchema>

const snapshotSchema = z.object({
  key: derivedHistoryKeySchema,
  value: z.number().finite().nullable(),
  unit: z.string().min(1),
  as_of: z.iso.datetime({ offset: true }),
  methodology_version: z.string().min(1),
  formula: z.string().min(1),
  inputs: z.record(z.string(), z.unknown()),
  reason: z.string().nullable(),
  reference_date: z.iso.date(),
})

const historyPointSchema = z.object({
  month: z.string().regex(/^\d{4}-(0[1-9]|1[0-2])$/),
  snapshot: snapshotSchema,
})

const historyWindowSchema = z.object({
  start_month: z.string().regex(/^\d{4}-(0[1-9]|1[0-2])$/),
  end_month: z.string().regex(/^\d{4}-(0[1-9]|1[0-2])$/),
  cutoff: z.iso.datetime({ offset: true }),
})

export const derivedHistoryResponseSchema = z.object({
  status: z.enum(['ok', 'unavailable']),
  window: historyWindowSchema,
  data: z.record(derivedHistoryKeySchema, z.array(historyPointSchema)),
}).superRefine((payload, context) => {
  if (payload.window.start_month > payload.window.end_month) {
    context.addIssue({ code: 'custom', path: ['window'], message: 'History window is reversed.' })
  }

  let hasPoints = false
  DERIVED_HISTORY_KEYS.forEach(key => {
    const points = payload.data[key]
    if (points.length > 0) hasPoints = true
    points.forEach((point, index) => {
      if (point.snapshot.key !== key || point.snapshot.reference_date.slice(0, 7) !== point.month) {
        context.addIssue({
          code: 'custom',
          path: ['data', key, index],
          message: 'History point does not match its series or month.',
        })
      }
      if (point.month < payload.window.start_month || point.month > payload.window.end_month) {
        context.addIssue({
          code: 'custom',
          path: ['data', key, index, 'month'],
          message: 'History point is outside the declared window.',
        })
      }
    })
  })

  if (payload.status === 'ok' !== hasPoints) {
    context.addIssue({ code: 'custom', path: ['status'], message: 'History status does not match its data.' })
  }
})

export type DerivedHistoryResponse = z.infer<typeof derivedHistoryResponseSchema>
export type DerivedHistoryPoint = z.infer<typeof historyPointSchema>
