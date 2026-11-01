import { z } from 'zod'

const timestamp = z.string().datetime({ offset: true }).nullable()
const envelope = { generated_at: z.string().datetime({ offset: true }), as_of: z.string().datetime({ offset: true }), scope: z.string().min(1) }
const taskCounts = z.record(z.string(), z.record(z.enum(['pending', 'claimed', 'succeeded', 'partial', 'failed', 'skipped', 'metadata_unknown']), z.number().int().nonnegative()))
const run = z.object({
  run_id: z.string().min(1).max(128), cycle_id: z.string().max(128).nullable(), state: z.enum(['running', 'succeeded', 'partial', 'failed', 'metadata_unknown']),
  created_at: timestamp, updated_at: timestamp, finished_at: timestamp, duration_seconds: z.number().nonnegative().nullable(),
  manifest_version: z.string().max(80), force: z.boolean(), task_counts: taskCounts, workflow_url: z.string().url().nullable(),
}).strict()

export const overviewSchema = z.object({ ...envelope, latest_attempt: run.nullable(), latest_fully_successful: run.nullable(), active_runs: z.object({ count: z.number().int().nonnegative(), scope: z.string() }).strict(), latest_valid_captures: z.record(z.string(), timestamp), state: z.enum(['recorded', 'no_backend_run_record']), provider_capture_scope: z.string() }).strict()
export const providersSchema = z.object({ ...envelope, scope: z.string(), items: z.array(z.object({
  provider: z.enum(['bcb_sgs', 'focus', 'tesouro', 'anbima', 'b3', 'ibge_sidra']), scope_id: z.string(),
  circuit: z.object({ recorded_state: z.enum(['closed', 'open', 'half_open', 'not_recorded', 'metadata_unknown']), opened_until: timestamp, retry_not_before: timestamp, probe_lease_until: timestamp, admission_view: z.enum(['next_admission_reassessment_due', 'temporarily_deferred', 'probe_lease_active', 'not_deferred', 'metadata_unknown']), as_of: z.string().datetime({ offset: true }) }).strict(),
  last_valid_capture_at: timestamp, capture_status: z.enum(['recorded', 'not_recorded']), latest_acquisition_failure_category: z.string().nullable().optional(), diagnostics_status: z.string(),
}).strict()) }).strict()

const task = z.object({ item_key: z.string(), task: z.string(), task_order: z.number().int().nonnegative(), status: z.enum(['pending', 'claimed', 'succeeded', 'partial', 'failed', 'skipped', 'metadata_unknown']), attempt_count: z.number().int().nonnegative().nullable(), claimed_at: timestamp, created_at: timestamp, finished_at: timestamp, duration_seconds: z.number().nonnegative().nullable(), series_updated: z.number().int().nonnegative().nullable(), record_count: z.number().int().nonnegative().nullable(), next_attempt_at: timestamp, deferral_reason: z.string().nullable(), last_error_category: z.string().nullable(), error_code: z.string().nullable() }).strict()
const taskPage = z.object({ items: z.array(task), total: z.number().int().nonnegative(), counts_by_status: z.record(z.string(), z.number().int().nonnegative()), limit: z.number().int().min(1).max(100), has_more: z.boolean(), next_cursor: z.number().int().nonnegative().nullable() }).strict()
export const runsSchema = z.object({ ...envelope, items: z.array(run), limit: z.number().int().min(1).max(100), has_more: z.boolean(), next_cursor: z.string().nullable() }).strict()
export const runDetailSchema = z.object({ ...envelope, scope: z.string(), run: z.object({ ...run.shape, derived_cutoff: timestamp, macro_cutoff: timestamp, recorded_final_summary: z.object({ status: z.string(), task_count: z.number().int().nonnegative().nullable() }).strict(), sgs_plan: z.object({ version: z.string(), as_of: timestamp, sha256: z.string().nullable() }).strict() }).strict(), task_page: taskPage, diagnostics: z.object({ status: z.string() }).passthrough(), current_freshness: z.object({ status: z.literal('not_included'), reason: z.string() }).strict() }).strict()

const interval = z.object({ requested_start: z.string().nullable(), requested_end: z.string().nullable(), reference_start: z.string().nullable(), reference_end: z.string().nullable(), captured_at: timestamp, basis: z.string() }).strict()
const coverageItem = z.object({
  series_code: z.number().int(), series_key: z.string(), name: z.string(), frequency: z.string(), unit: z.string(), latest_reference: z.string().nullable(), last_valid_capture_at: timestamp,
  capture_search: z.object({ status: z.enum(['metadata_unknown', 'recorded', 'not_recorded']), candidate_budget: z.number().int(), candidates_examined: z.number().int(), budget_sentinel_present: z.boolean() }).strict(),
  freshness_days: z.number().int().nonnegative(), configured_start: z.string().nullable(), documented_start: z.string().nullable(), first_reference_period: z.string().nullable(), coverage_status: z.enum(['metadata_unknown', 'evidence_available']), coverage_reason: z.string().nullable(), status_flags: z.array(z.string()), evidence_basis: z.string(), acquired_proof_intervals: z.array(interval), proof_intervals_truncated: z.boolean(),
  proof_search: z.object({ status: z.enum(['metadata_unknown', 'complete']), candidate_budget: z.number().int(), candidates_examined: z.number().int(), budget_sentinel_present: z.boolean() }).strict(), history_status: z.string(), as_of: z.string().datetime({ offset: true }),
}).strict()
export const coverageSchema = z.object({ ...envelope, provider: z.enum(['bcb_sgs', 'focus', 'tesouro', 'anbima', 'b3', 'ibge_sidra']), coverage_status: z.string(), reason: z.string().optional(), latest_valid_capture_at: timestamp.optional(), capture_status: z.enum(['recorded', 'not_recorded']).optional(), capture_basis: z.string().optional(), latest_reference: z.string().nullable().optional(), latest_reference_captured_at: timestamp.optional(), latest_reference_basis: z.string().optional(), item_count: z.number().int().nonnegative().nullable().optional(), item_count_status: z.string().optional(), items_status: z.string().optional(), items: z.array(coverageItem), limit: z.number().int().min(1).max(100), has_more: z.boolean(), next_cursor: z.union([z.number().int().nonnegative(), z.null()]), cursor_order: z.string().optional() }).strict()

export type Overview = z.infer<typeof overviewSchema>
export type TaskCounts = z.infer<typeof taskCounts>
export type Providers = z.infer<typeof providersSchema>
export type RunsPage = z.infer<typeof runsSchema>
export type RunDetail = z.infer<typeof runDetailSchema>
export type Coverage = z.infer<typeof coverageSchema>

export class PrivateApiError extends Error {
  constructor(readonly kind: 'unauthorized' | 'forbidden' | 'unavailable' | 'invalid' | 'notfound', readonly status?: number) { super(kind) }
}

export async function getPrivate<T>(path: string, schema: z.ZodType<T>, signal: AbortSignal): Promise<T> {
  if (!path.startsWith('/api/admin/') || path.startsWith('//')) throw new PrivateApiError('invalid')
  let response: Response
  try {
    response = await fetch(path, { method: 'GET', credentials: 'same-origin', cache: 'no-store', headers: { Accept: 'application/json' }, signal })
  } catch {
    throw new PrivateApiError('unavailable')
  }
  if (response.status === 401) throw new PrivateApiError('unauthorized', 401)
  if (response.status === 403) throw new PrivateApiError('forbidden', 403)
  if (response.status === 404) throw new PrivateApiError('notfound', 404)
  if (response.status === 422) throw new PrivateApiError('invalid', 422)
  if (!response.ok) throw new PrivateApiError('unavailable', response.status)
  let value: unknown
  try { value = await response.json() } catch { throw new PrivateApiError('invalid', response.status) }
  const parsed = schema.safeParse(value)
  if (!parsed.success) throw new PrivateApiError('invalid', response.status)
  return parsed.data
}
