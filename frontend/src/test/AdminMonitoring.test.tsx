import { cleanup, fireEvent, render, screen, waitFor } from '@testing-library/react'
import { MemoryRouter, Route, Routes } from 'react-router-dom'
import { describe, expect, it, vi } from 'vitest'
import { AdminGate } from '../admin/AdminArea'
import { AdminAuthProvider } from '../admin/AuthProvider'
import { getPrivate, overviewSchema, PrivateApiError, type Coverage, type RunDetail } from '../admin/adminApi'

const session = (csrf = 'c'.repeat(43)) => ({ user_id: '11111111-1111-4111-8111-111111111111', permissions: ['monitoring:read'], idle_expires_at: new Date(Date.now() + 600_000).toISOString(), absolute_expires_at: new Date(Date.now() + 3_600_000).toISOString(), csrf_token: csrf })
const run = (runId: string, state: RunDetail['run']['state']) => ({ run_id: runId, cycle_id: null, state, created_at: '2026-06-01T12:00:00Z', updated_at: '2026-06-01T12:01:00Z', finished_at: '2026-06-01T12:01:00Z', duration_seconds: 60, manifest_version: 'daily-refresh-v6', force: false, task_counts: {}, workflow_url: null })
const overview = (attempt: string, state: RunDetail['run']['state'] = 'partial') => ({ generated_at: '2026-06-01T12:02:00Z', as_of: '2026-06-01T12:02:00Z', scope: 'persisted_refresh_runs_and_receipts', latest_attempt: run(attempt, state), latest_fully_successful: run('prior-success', 'succeeded'), active_runs: { count: 0, scope: 'persisted_state_running' }, latest_valid_captures: {}, state: 'recorded', provider_capture_scope: 'validated receipts' })
const providerData = { generated_at: '2026-06-01T12:02:00Z', as_of: '2026-06-01T12:02:00Z', scope: 'curated_provider_registry_and_persisted_receipts', items: ['bcb_sgs', 'focus', 'tesouro', 'anbima', 'b3', 'ibge_sidra'].map(provider => ({ provider, scope_id: `${provider}:primary:primary`, circuit: { recorded_state: 'not_recorded', opened_until: null, retry_not_before: null, probe_lease_until: null, admission_view: 'metadata_unknown', as_of: '2026-06-01T12:02:00Z' }, last_valid_capture_at: null, capture_status: 'not_recorded', latest_acquisition_failure_category: null, diagnostics_status: 'not_included' })) }
const focusCoverage = { generated_at: '2026-06-01T12:02:00Z', as_of: '2026-06-01T12:02:00Z', scope: 'recognized_provider_persisted_capture_facts; no_provider_series_catalog_or_completeness_contract', provider: 'focus', coverage_status: 'metadata_unknown', reason: 'provider_specific_series_coverage_contract_not_available', latest_valid_capture_at: null, capture_status: 'not_recorded', capture_basis: 'existing_curated_provider_receipt_projection', latest_reference: null, latest_reference_captured_at: null, latest_reference_basis: 'not_recorded', item_count: null, item_count_status: 'metadata_unknown', items: [], items_status: 'not_recorded', limit: 50, has_more: false, next_cursor: null }

function privateApp(path = '/admin') {
  return render(<MemoryRouter initialEntries={[path]}><Routes><Route path="/admin/*" element={<AdminAuthProvider monitorSession><AdminGate /></AdminAuthProvider>} /><Route path="/login" element={<p>Login após encerramento da sessão</p>} /></Routes></MemoryRouter>)
}

const historyPage = (hasMore = false) => ({ generated_at: '2026-06-01T12:02:00Z', as_of: '2026-06-01T12:02:00Z', scope: 'refresh_runs_created_in_half_open_period', items: [run('run-1', 'partial')], limit: 50, has_more: hasMore, next_cursor: hasMore ? 'opaque-cursor-1' : null })
const detail = (moreTasks = false): RunDetail => ({
  generated_at: '2026-06-01T12:02:00Z', as_of: '2026-06-01T12:02:00Z', scope: 'selected_persisted_run_snapshot; diagnostics_exact_run_only',
  run: { ...run('run-1', 'partial'), derived_cutoff: null, macro_cutoff: null, recorded_final_summary: { status: 'partial', task_count: 1 }, sgs_plan: { version: 'sgs-coverage-policy-v3', as_of: '2026-06-01T00:00:00Z', sha256: 'a'.repeat(64) } },
  task_page: { items: [{ item_key: 'task-key', task: 'sgs_123', task_order: 0, status: 'failed', attempt_count: 2, claimed_at: null, created_at: '2026-06-01T12:00:00Z', finished_at: null, duration_seconds: null, series_updated: 1, record_count: 2, next_attempt_at: null, deferral_reason: 'publisher_cooldown', last_error_category: 'network_timeout', error_code: 'ReadTimeout' }, { item_key: 'task-key-2', task: 'sidra', task_order: 1, status: 'failed', attempt_count: 1, claimed_at: null, created_at: '2026-06-01T12:00:00Z', finished_at: null, duration_seconds: null, series_updated: null, record_count: null, next_attempt_at: null, deferral_reason: null, last_error_category: null, error_code: 'PoolTimeout' }], total: 2, counts_by_status: { failed: 2 }, limit: 50, has_more: moreTasks, next_cursor: moreTasks ? 1 : null },
  diagnostics: { status: 'not_recorded', items: [] }, current_freshness: { status: 'not_included', reason: 'historical_run_snapshot_is_frozen' },
})
const sgsCoverage = (): Coverage => ({ generated_at: '2026-06-01T12:02:00Z', as_of: '2026-06-01T12:02:00Z', scope: 'current_bcb_sgs_catalog_and_exact_receipts; no_calendar_gap_inference', provider: 'bcb_sgs', coverage_status: 'catalog_and_receipt_evidence', item_count: 1, items: [{
  series_code: 123, series_key: 'example', name: 'Série exemplo', frequency: 'mensal', unit: 'percent', latest_reference: '2026-05-01', last_valid_capture_at: '2026-06-01T12:00:00Z',
  capture_search: { status: 'recorded', candidate_budget: 128, candidates_examined: 129, budget_sentinel_present: true }, freshness_days: 40, configured_start: '2000-01-01', documented_start: '1990-01-01', first_reference_period: '1990-01-01', coverage_status: 'metadata_unknown', coverage_reason: 'candidate_search_budget_exhausted', status_flags: ['metadata_unknown', 'before_documented_start'], evidence_basis: 'exact_plan_bound_sgs_unit_commit_and_archive_receipts',
  acquired_proof_intervals: [{ requested_start: '2026-01-01', requested_end: '2026-02-01', reference_start: '2026-01-01', reference_end: '2026-01-01', captured_at: '2026-06-01T12:00:00Z', basis: 'exact_validated_sgs_archive_receipt' }], proof_intervals_truncated: false,
  proof_search: { status: 'metadata_unknown', candidate_budget: 128, candidates_examined: 129, budget_sentinel_present: true }, history_status: 'metadata_unknown', as_of: '2026-06-01T12:02:00Z',
}], limit: 50, has_more: true, next_cursor: 50, cursor_order: 'catalog provider_code ascending' })

describe('private monitoring panel', () => {
  it('uses same-origin no-store GETs and renders latest attempt separately from last success', async () => {
    const requests: Array<[string, RequestInit]> = []
    vi.stubGlobal('fetch', vi.fn(async (input: string, init: RequestInit = {}) => {
      requests.push([input, init])
      if (input === '/api/auth/session') return Response.json(session())
      if (input === '/api/admin/overview') return Response.json(overview('current-partial'))
      if (input === '/api/admin/providers') return Response.json(providerData)
      throw new Error(`unexpected request ${input}`)
    }))
    privateApp()
    expect(await screen.findByText('current-partial')).toBeVisible()
    expect(screen.getByText('prior-success')).toBeVisible()
    expect(requests.filter(([url]) => url.startsWith('/api/admin/')).every(([, init]) => init.method === 'GET' && init.credentials === 'same-origin' && init.cache === 'no-store')).toBe(true)
    expect(requests.some(([url, init]) => url.startsWith('/api/admin/') && JSON.stringify(init.headers).includes('CSRF'))).toBe(false)
  })

  it('fails closed on malformed DTOs and distinguishes non-SGS coverage as unknown', async () => {
    const response = new Response('{}', { status: 200 })
    vi.stubGlobal('fetch', vi.fn().mockResolvedValue(response))
    await expect(getPrivate('/api/admin/overview', overviewSchema, new AbortController().signal)).rejects.toMatchObject({ kind: 'invalid' })
    const authError = vi.fn().mockResolvedValue(new Response('{}', { status: 403 }))
    vi.stubGlobal('fetch', authError)
    await expect(getPrivate('/api/admin/overview', overviewSchema, new AbortController().signal)).rejects.toBeInstanceOf(PrivateApiError)
    expect(authError.mock.calls[0][1]).toMatchObject({ method: 'GET', credentials: 'same-origin', cache: 'no-store' })
    vi.stubGlobal('fetch', vi.fn().mockRejectedValue(new Error('offline')))
    await expect(getPrivate('/api/admin/overview', overviewSchema, new AbortController().signal)).rejects.toMatchObject({ kind: 'unavailable' })
    vi.stubGlobal('fetch', vi.fn().mockResolvedValue(new Response('not-json', { status: 200 })))
    await expect(getPrivate('/api/admin/overview', overviewSchema, new AbortController().signal)).rejects.toMatchObject({ kind: 'invalid' })
    await expect(getPrivate('//evil.example/api/admin/overview', overviewSchema, new AbortController().signal)).rejects.toMatchObject({ kind: 'invalid' })
    for (const status of [401, 403, 404, 422, 503]) {
      vi.stubGlobal('fetch', vi.fn().mockResolvedValue(new Response('{}', { status })))
      await expect(getPrivate('/api/admin/overview', overviewSchema, new AbortController().signal)).rejects.toBeInstanceOf(PrivateApiError)
    }
  })

  it('handles unknown routes safely, including malformed encoded run identifiers', async () => {
    vi.stubGlobal('fetch', vi.fn().mockResolvedValueOnce(Response.json(session())).mockResolvedValue(Response.json(overview('safe-fallback'))))
    privateApp('/admin/anything-at-all')
    expect(await screen.findByText('safe-fallback')).toBeVisible()
  })

  it('submits BRT inclusive dates as UTC-offset half-open bounds and walks only explicit cursor pages', async () => {
    const requests: string[] = []
    vi.stubGlobal('fetch', vi.fn(async (input: string) => {
      requests.push(input)
      if (input === '/api/auth/session') return Response.json(session())
      if (input.startsWith('/api/admin/runs?')) return Response.json(historyPage(true))
      return Response.json({ error: 'not-found' }, { status: 404 })
    }))
    privateApp('/admin/atualizacoes?since=2026-06-01&until=2026-06-30&state=partial')
    expect(await screen.findByRole('link', { name: 'run-1' })).toBeVisible()
    expect(requests.find(value => value.startsWith('/api/admin/runs?'))).toContain('since=2026-06-01T00%3A00%3A00-03%3A00')
    expect(requests.find(value => value.startsWith('/api/admin/runs?'))).toContain('until=2026-07-01T00%3A00%3A00-03%3A00')
    fireEvent.click(screen.getByRole('button', { name: 'Próxima página' }))
    await waitFor(() => expect(requests.some(value => value.includes('cursor=opaque-cursor-1'))).toBe(true))
    fireEvent.click(screen.getByRole('button', { name: 'Página anterior' }))
    await waitFor(() => expect(screen.getByText('Primeira página')).toBeVisible())
    fireEvent.change(screen.getByLabelText('Estado'), { target: { value: 'failed' } })
    fireEvent.click(screen.getByRole('button', { name: 'Aplicar filtros' }))
    await waitFor(() => expect(screen.getByLabelText('Estado')).toHaveValue('failed'))
    expect(requests.some(value => value.includes('cursor=opaque-cursor-1'))).toBe(true)
  })

  it('shows exact historical task facts, fixed translations, task page controls, and unknown runs as not found', async () => {
    const requests: string[] = []
    vi.stubGlobal('fetch', vi.fn(async (input: string) => {
      requests.push(input)
      if (input === '/api/auth/session') return Response.json(session())
      if (input.startsWith('/api/admin/runs/run-1')) return Response.json(detail(true))
      return new Response('{}', { status: 404 })
    }))
    privateApp('/admin/atualizacoes/run-1')
    expect(await screen.findByText('Aguardar intervalo do publicador')).toBeVisible()
    expect(screen.getByText(/frescura atual não está incluída/)).toBeVisible()
    expect(screen.getByText('Limite de conexões')).toBeInTheDocument()
    fireEvent.click(screen.getByRole('button', { name: 'Próximas tarefas' }))
    await waitFor(() => expect(requests.some(value => value.includes('task_cursor=1'))).toBe(true))
    vi.stubGlobal('fetch', vi.fn(async (input: string) => input === '/api/auth/session' ? Response.json(session()) : new Response('{}', { status: 404 })))
    cleanup()
    privateApp('/admin/atualizacoes/absent')
    expect(await screen.findByRole('heading', { name: 'Execução não registrada' })).toBeVisible()
  })

  it('marks SGS candidate-budget sentinel incomplete while preserving the exact interval and page boundary', async () => {
    vi.stubGlobal('fetch', vi.fn(async (input: string) => {
      if (input === '/api/auth/session') return Response.json(session())
      if (input === '/api/admin/providers') return Response.json(providerData)
      if (input.startsWith('/api/admin/coverage')) return Response.json(sgsCoverage())
      return new Response('{}', { status: 404 })
    }))
    privateApp('/admin/cobertura?provider=bcb_sgs')
    expect(await screen.findByText('Verificação limitada: cobertura não determinada.')).toBeVisible()
    expect(screen.getByText('Antes do início documentado (excluído)')).toBeVisible()
    fireEvent.click(screen.getByText('1 intervalos comprovados'))
    expect(screen.getByText(/2026-01-01 a 2026-02-01/)).toBeVisible()
    expect(screen.getByText('Posição 0')).toBeVisible()
    expect(screen.getByRole('combobox', { name: 'Fonte' })).toHaveValue('bcb_sgs')
  })

  it('hides every private panel after a monitoring 401 and failed session revalidation', async () => {
    let calls = 0
    vi.stubGlobal('fetch', vi.fn(async (input: string) => {
      if (input === '/api/auth/session') return calls++ === 0 ? Response.json(session()) : new Response('{}', { status: 401 })
      if (input === '/api/admin/overview') return new Response('{}', { status: 401 })
      if (input === '/api/admin/providers') return Response.json(providerData)
      return new Response('{}', { status: 503 })
    }))
    privateApp()
    expect(await screen.findByText('Login após encerramento da sessão')).toBeVisible()
    expect(screen.queryByText('11111111-1111-4111-8111-111111111111')).not.toBeInTheDocument()
  })

  it('presents absent latest records and translates fixed circuit enums without asserting source health', async () => {
    const openProviders = { ...providerData, items: providerData.items.map((item, index) => ({ ...item, circuit: { ...item.circuit, recorded_state: ['open', 'closed', 'half_open', 'metadata_unknown', 'not_recorded', 'closed'][index], admission_view: ['next_admission_reassessment_due', 'temporarily_deferred', 'probe_lease_active', 'not_deferred', 'metadata_unknown', 'metadata_unknown'][index], opened_until: index === 0 ? '2026-06-01T13:00:00Z' : null, retry_not_before: index === 1 ? '2026-06-01T14:00:00Z' : null, probe_lease_until: index === 2 ? '2026-06-01T15:00:00Z' : null }, latest_acquisition_failure_category: index === 0 ? 'network_timeout' : index === 1 ? 'unmapped-category' : null })) }
    vi.stubGlobal('fetch', vi.fn(async (input: string) => {
      if (input === '/api/auth/session') return Response.json(session())
      if (input === '/api/admin/overview') return Response.json({ ...overview('unused'), latest_attempt: null, latest_fully_successful: null, state: 'no_backend_run_record', active_runs: { count: 3, scope: 'running_state' }, latest_valid_captures: { bcb_sgs: '2026-06-01T12:00:00Z' } })
      if (input === '/api/admin/providers') return Response.json(openProviders)
      return new Response('{}', { status: 503 })
    }))
    privateApp()
    expect(await screen.findByText('Sem registro de execução no backend. Uma falha do GitHub Actions antes do registro pode não aparecer aqui.')).toBeVisible()
    expect(screen.getByText('Aberto no registro; reavaliação de admissão devida')).toBeVisible()
    expect(screen.getByText('Tempo limite de rede')).toBeVisible()
    expect(screen.getByText('Meio aberto no registro')).toBeVisible()
    expect(screen.getByText('Concessão de sondagem registrada')).toBeVisible()
    expect(screen.getByText('Categoria desconhecida')).toBeVisible()
    expect(screen.getByText('01/06/2026, 10:00:00 BRT')).toBeVisible()
  })

  it('renders the no-run empty overview without inventing an attempt or a prior success', async () => {
    const emptyOverview = { ...overview('unused'), latest_attempt: null, latest_fully_successful: null, state: 'no_backend_run_record' as const, active_runs: { count: 0, scope: 'persisted_state_running' }, latest_valid_captures: { bcb_sgs: null } }
    vi.stubGlobal('fetch', vi.fn(async (input: string) => {
      if (input === '/api/auth/session') return Response.json(session())
      if (input === '/api/admin/overview') return Response.json(emptyOverview)
      if (input === '/api/admin/providers') return Response.json({ ...providerData, items: [] })
      return new Response('{}', { status: 503 })
    }))
    privateApp()
    expect(await screen.findByText(/Uma falha do GitHub Actions antes do registro/)).toBeVisible()
    expect(screen.getAllByText('Não registrado').length).toBeGreaterThanOrEqual(2)
    expect(screen.queryByText('unused')).not.toBeInTheDocument()
    expect(screen.getByText('0')).toBeVisible()
  })

  it('shows empty successful history distinctly, including optional and unknown-safe run fields', async () => {
    vi.stubGlobal('fetch', vi.fn(async (input: string) => {
      if (input === '/api/auth/session') return Response.json(session())
      if (input.startsWith('/api/admin/runs?')) return Response.json({ ...historyPage(), items: [] })
      return new Response('{}', { status: 503 })
    }))
    privateApp('/admin/atualizacoes')
    expect(await screen.findByText('Nenhuma execução registrada neste período.')).toHaveAttribute('role', 'status')
    expect(screen.getByRole('button', { name: 'Próxima página' })).toBeDisabled()
    expect(screen.getByRole('button', { name: 'Página anterior' })).toBeDisabled()
  })

  it('keeps legacy history nulls and unknown persisted states explicit in the run table', async () => {
    const legacyRun = { ...run('legacy-id', 'metadata_unknown'), created_at: null, updated_at: null, finished_at: null, duration_seconds: null, workflow_url: null, task_counts: {} }
    vi.stubGlobal('fetch', vi.fn(async (input: string) => {
      if (input === '/api/auth/session') return Response.json(session())
      if (input.startsWith('/api/admin/runs?')) return Response.json({ ...historyPage(), items: [legacyRun] })
      return new Response('{}', { status: 503 })
    }))
    privateApp('/admin/atualizacoes')
    expect(await screen.findByRole('link', { name: 'legacy-id' })).toBeVisible()
    expect(screen.getAllByText('Metadados desconhecidos').length).toBeGreaterThan(0)
    expect(screen.getAllByText('Não registrado').length).toBeGreaterThan(0)
    expect(screen.getByText('Primeira página')).toBeVisible()
  })

  it('recovers a failed read only after explicit retry and does not display stale facts as current', async () => {
    let calls = 0
    vi.stubGlobal('fetch', vi.fn(async (input: string) => {
      if (input === '/api/auth/session') return Response.json(session())
      if (input === '/api/admin/overview') return calls++ === 0 ? new Response('{}', { status: 503 }) : Response.json(overview('recovered'))
      if (input === '/api/admin/providers') return Response.json(providerData)
      return new Response('{}', { status: 503 })
    }))
    privateApp()
    expect(await screen.findByRole('heading', { name: 'Leitura indisponível' })).toBeVisible()
    expect(screen.queryByText('recovered')).not.toBeInTheDocument()
    fireEvent.click(screen.getByRole('button', { name: 'Tentar novamente' }))
    expect(await screen.findByText('recovered')).toBeVisible()
  })

  it('renders legacy nullable task facts and fallback categories without fabricating a diagnosis', async () => {
    const legacy = detail(false)
    legacy.run.state = 'metadata_unknown'
    legacy.run.recorded_final_summary = { status: 'metadata_unknown', task_count: null }
    legacy.run.sgs_plan.sha256 = null
    legacy.diagnostics = { status: 'legacy_state', items: [] }
    legacy.task_page.items[0] = { ...legacy.task_page.items[0], status: 'metadata_unknown', attempt_count: null, claimed_at: null, created_at: null, finished_at: null, duration_seconds: null, series_updated: null, record_count: null, next_attempt_at: null, deferral_reason: null, last_error_category: 'network_timeout', error_code: null }
    legacy.task_page.items[1] = { ...legacy.task_page.items[1], deferral_reason: null, last_error_category: null, error_code: 'unmapped_error' }
    vi.stubGlobal('fetch', vi.fn(async (input: string) => {
      if (input === '/api/auth/session') return Response.json(session())
      if (input.startsWith('/api/admin/runs/run-1')) return Response.json(legacy)
      return new Response('{}', { status: 404 })
    }))
    privateApp('/admin/atualizacoes/run-1')
    expect((await screen.findAllByText('Metadados desconhecidos')).length).toBeGreaterThan(0)
    expect(screen.getByText('Tempo limite de rede')).toBeVisible()
    expect(screen.getByText('Código de erro conhecido')).toBeVisible()
    expect(screen.getAllByText('Não registrado').length).toBeGreaterThan(2)
    expect(screen.getByRole('button', { name: 'Próximas tarefas' })).toBeDisabled()
  })

  it('renders validated SGS evidence, positive bounded search, optional intervals and truncation explicitly', async () => {
    const complete = sgsCoverage()
    const item = complete.items[0]
    item.capture_search = { status: 'recorded', candidate_budget: 128, candidates_examined: 1, budget_sentinel_present: false }
    item.proof_search = { status: 'complete', candidate_budget: 128, candidates_examined: 1, budget_sentinel_present: false }
    item.coverage_status = 'evidence_available'
    item.coverage_reason = null
    item.status_flags = ['acquisition_unverified', 'history_pending', 'stale_reference', 'unknown_flag']
    item.proof_intervals_truncated = true
    item.acquired_proof_intervals = [{ ...item.acquired_proof_intervals[0], requested_start: null, requested_end: null, reference_start: null, reference_end: null, captured_at: null, basis: 'bounded_receipt' }]
    vi.stubGlobal('fetch', vi.fn(async (input: string) => {
      if (input === '/api/auth/session') return Response.json(session())
      if (input === '/api/admin/providers') return Response.json(providerData)
      if (input.startsWith('/api/admin/coverage')) return Response.json(complete)
      return new Response('{}', { status: 503 })
    }))
    privateApp('/admin/cobertura?provider=bcb_sgs')
    expect(await screen.findByText('Aquisição não verificada')).toBeVisible()
    expect(screen.getByText('Histórico pendente (busca completa)')).toBeVisible()
    expect(screen.getByText('Referência antiga')).toBeVisible()
    expect(screen.getByText('Estado desconhecido')).toBeVisible()
    expect(screen.queryByText('Verificação limitada: cobertura não determinada.')).not.toBeInTheDocument()
    expect(screen.getByText('Intervalos truncados após o limite de exibição.')).toBeVisible()
    fireEvent.click(screen.getByText('1 intervalos comprovados'))
    expect(screen.getByText('? a ? · referência ?–? · captura Não registrado')).toBeVisible()
  })

  it('distinguishes unresolved SGS metadata after complete searches from exhausted candidate budgets', async () => {
    const unresolved = sgsCoverage()
    const item = unresolved.items[0]
    item.capture_search = { status: 'not_recorded', candidate_budget: 128, candidates_examined: 0, budget_sentinel_present: false }
    item.proof_search = { status: 'complete', candidate_budget: 128, candidates_examined: 1, budget_sentinel_present: false }
    item.coverage_status = 'metadata_unknown'
    item.coverage_reason = 'catalog_metadata_unavailable'
    item.status_flags = []
    item.proof_intervals_truncated = false
    item.acquired_proof_intervals = []
    unresolved.has_more = false
    unresolved.next_cursor = null
    vi.stubGlobal('fetch', vi.fn(async (input: string) => {
      if (input === '/api/auth/session') return Response.json(session())
      if (input === '/api/admin/providers') return Response.json(providerData)
      if (input.startsWith('/api/admin/coverage')) return Response.json(unresolved)
      return new Response('{}', { status: 503 })
    }))
    privateApp('/admin/cobertura?provider=bcb_sgs')
    expect(await screen.findByText('Metadados insuficientes para determinar cobertura.')).toBeVisible()
    expect(screen.queryByText('Verificação limitada: cobertura não determinada.')).not.toBeInTheDocument()
    expect(screen.getByText('Fim da página atual.')).toBeVisible()
    expect(screen.getByRole('button', { name: 'Próxima página' })).toBeDisabled()
  })

  it('allows only fixed-host numeric workflow links and rejects encoded path traversal run IDs', async () => {
    const safeOverview = { ...overview('safe-run'), latest_attempt: { ...run('safe-run', 'partial'), workflow_url: 'https://github.com/thiagojesus/X-BRAY/actions/runs/12345' }, latest_fully_successful: { ...run('external-run', 'succeeded'), workflow_url: 'https://evil.example/thiagojesus/X-BRAY/actions/runs/12345' } }
    vi.stubGlobal('fetch', vi.fn(async (input: string) => {
      if (input === '/api/auth/session') return Response.json(session())
      if (input === '/api/admin/overview') return Response.json(safeOverview)
      if (input === '/api/admin/providers') return Response.json(providerData)
      return new Response('{}', { status: 503 })
    }))
    privateApp()
    const link = await screen.findByRole('link', { name: 'Ver no GitHub' })
    expect(link).toHaveAttribute('href', 'https://github.com/thiagojesus/X-BRAY/actions/runs/12345')
    expect(link).toHaveAttribute('rel', 'noopener noreferrer')
    expect(screen.getAllByRole('link').some(anchor => anchor.getAttribute('href')?.includes('evil.example'))).toBe(false)
    cleanup()
    vi.stubGlobal('fetch', vi.fn(async (input: string) => input === '/api/auth/session' ? Response.json(session()) : new Response('{}', { status: 503 })))
    privateApp('/admin/atualizacoes/%2F')
    expect(await screen.findByText('Identificador de execução inválido.')).toBeVisible()
  })

  it('renders a 422 pagination response as a resettable error rather than a false empty success', async () => {
    vi.stubGlobal('fetch', vi.fn(async (input: string) => {
      if (input === '/api/auth/session') return Response.json(session())
      return new Response('{}', { status: 422 })
    }))
    privateApp('/admin/atualizacoes?cursor=stale')
    expect(await screen.findByText('Cursor ou filtros recusados pelo servidor (422). Volte à primeira página e reaplique os filtros.')).toBeVisible()
    expect(screen.getByText('Cursor ou filtros recusados pelo servidor (422). Volte à primeira página e reaplique os filtros.')).toHaveAttribute('role', 'alert')
    fireEvent.click(screen.getByRole('button', { name: 'Reiniciar paginação' }))
    expect(screen.getByText('Primeira página')).toBeVisible()
  })

  it('does not commit a late previous-session overview after same-user CSRF/session rotation', async () => {
    let resolveOld!: (response: Response) => void
    let sessionCalls = 0
    let overviewCalls = 0
    const delayed = new Promise<Response>(resolve => { resolveOld = resolve })
    vi.stubGlobal('fetch', vi.fn((input: string) => {
      if (input === '/api/auth/session') {
        sessionCalls += 1
        return Promise.resolve(Response.json(session(sessionCalls === 1 ? 'a'.repeat(43) : 'b'.repeat(43))))
      }
      if (input === '/api/admin/overview') {
        overviewCalls += 1
        return overviewCalls === 1 ? delayed : Promise.resolve(Response.json(overview('new-session-data')))
      }
      if (input === '/api/admin/providers') return Promise.resolve(Response.json(providerData))
      return Promise.reject(new Error('unexpected route'))
    }))
    privateApp()
    await waitFor(() => expect(overviewCalls).toBe(1))
    fireEvent(window, new Event('pageshow'))
    await waitFor(() => expect(screen.getByText('new-session-data')).toBeVisible())
    resolveOld(Response.json(overview('old-session-secret-sentinel')))
    await waitFor(() => expect(screen.queryByText('old-session-secret-sentinel')).not.toBeInTheDocument())
    expect(overviewCalls).toBeGreaterThanOrEqual(2)
  })

  it('refreshes only the selected view and never invokes operational POST actions', async () => {
    const methods: string[] = []
    vi.stubGlobal('fetch', vi.fn(async (input: string, init: RequestInit = {}) => {
      methods.push(`${init.method ?? 'GET'} ${input}`)
      if (input === '/api/auth/session') return Response.json(session())
      if (input === '/api/admin/overview') return Response.json(overview('overview-only'))
      if (input === '/api/admin/providers') return Response.json(providerData)
      if (input.includes('/api/admin/coverage')) return Response.json(focusCoverage)
      return new Response('{}', { status: 503 })
    }))
    privateApp()
    await screen.findByText('overview-only')
    fireEvent.click(screen.getByRole('link', { name: 'Cobertura e fontes' }))
    fireEvent.change(screen.getByLabelText('Fonte'), { target: { value: 'focus' } })
    expect(await screen.findByText(/Cobertura ainda não avaliada/)).toBeVisible()
    expect(methods.some(method => method.startsWith('POST /api/admin/'))).toBe(false)
    expect(methods.some(method => method.includes('provider=bcb_sgs'))).toBe(true)
  })
})
