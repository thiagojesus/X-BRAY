import { expect, test, type Page } from '@playwright/test'

const validSession = () => ({
  user_id: '11111111-1111-4111-8111-111111111111', permissions: ['monitoring:read'],
  idle_expires_at: new Date(Date.now() + 10 * 60_000).toISOString(),
  absolute_expires_at: new Date(Date.now() + 60 * 60_000).toISOString(), csrf_token: 'e2e-csrf-token-value-which-is-not-a-secret',
})

const mockRun = (run_id: string, state: string) => ({ run_id, cycle_id: null, state, created_at: '2026-06-01T12:00:00Z', updated_at: '2026-06-01T12:01:00Z', finished_at: '2026-06-01T12:01:00Z', duration_seconds: 60, manifest_version: 'daily-refresh-v6', force: false, task_counts: {}, workflow_url: null })
const mockOverview = () => ({ generated_at: '2026-06-01T12:02:00Z', as_of: '2026-06-01T12:02:00Z', scope: 'persisted_refresh_runs_and_receipts', latest_attempt: mockRun('current-partial', 'partial'), latest_fully_successful: mockRun('prior-success', 'succeeded'), active_runs: { count: 0, scope: 'persisted_state_running' }, latest_valid_captures: {}, state: 'recorded', provider_capture_scope: 'receipt-backed' })
const mockProviders = () => ({ generated_at: '2026-06-01T12:02:00Z', as_of: '2026-06-01T12:02:00Z', scope: 'curated_provider_registry_and_persisted_receipts', items: ['bcb_sgs', 'focus', 'tesouro', 'anbima', 'b3', 'ibge_sidra'].map(provider => ({ provider, scope_id: `${provider}:primary:primary`, circuit: { recorded_state: 'not_recorded', opened_until: null, retry_not_before: null, probe_lease_until: null, admission_view: 'metadata_unknown', as_of: '2026-06-01T12:02:00Z' }, last_valid_capture_at: null, capture_status: 'not_recorded', latest_acquisition_failure_category: null, diagnostics_status: 'not_included' })) })
const mockRuns = () => ({ generated_at: '2026-06-01T12:02:00Z', as_of: '2026-06-01T12:02:00Z', scope: 'refresh_runs_created_in_half_open_period', items: [mockRun('current-partial', 'partial')], limit: 50, has_more: false, next_cursor: null })
const mockDetail = () => ({ generated_at: '2026-06-01T12:02:00Z', as_of: '2026-06-01T12:02:00Z', scope: 'selected_persisted_run_snapshot; diagnostics_exact_run_only', run: { ...mockRun('current-partial', 'partial'), derived_cutoff: null, macro_cutoff: null, recorded_final_summary: { status: 'partial', task_count: 1 }, sgs_plan: { version: 'not_recorded', as_of: null, sha256: null } }, task_page: { items: [], total: 0, counts_by_status: {}, limit: 50, has_more: false, next_cursor: null }, diagnostics: { status: 'not_recorded', items: [] }, current_freshness: { status: 'not_included', reason: 'historical_run_snapshot_is_frozen' } })
const focusCoverage = () => ({ generated_at: '2026-06-01T12:02:00Z', as_of: '2026-06-01T12:02:00Z', scope: 'recognized_provider_persisted_capture_facts; no_provider_series_catalog_or_completeness_contract', provider: 'focus', coverage_status: 'metadata_unknown', reason: 'provider_specific_series_coverage_contract_not_available', latest_valid_capture_at: null, capture_status: 'not_recorded', capture_basis: 'existing_curated_provider_receipt_projection', latest_reference: null, latest_reference_captured_at: null, latest_reference_basis: 'not_recorded', item_count: null, item_count_status: 'metadata_unknown', items: [], items_status: 'not_recorded', limit: 50, has_more: false, next_cursor: null })

async function installApiStubs(page: Page, sessionStatus: () => number, afterCallback?: () => void, afterLogout?: () => void) {
  await page.route('**/api/**', async route => {
    const url = new URL(route.request().url())
    if (url.pathname === '/api/auth/session') {
      const status = sessionStatus()
      await route.fulfill({ status, contentType: 'application/json', body: JSON.stringify(status === 200 ? validSession() : { error: status === 401 ? 'session_invalid' : 'auth_unavailable' }) })
      return
    }
    if (url.pathname === '/api/auth/login') {
      await route.fulfill({ status: 200, contentType: 'text/html; charset=utf-8', body: '<main><h1>Provedor Google simulado</h1><button onclick="location.href=\'/api/auth/callback?code=mock-code&flow_state=mock-state\'">Continuar (simulação)</button></main>' })
      return
    }
    if (url.pathname === '/api/auth/callback') {
      afterCallback?.()
      await page.context().addCookies([{
        name: 'xbray_admin_dev', value: 'mock-session', url: url.origin,
        httpOnly: true, sameSite: 'Lax',
      }])
      await route.fulfill({ status: 200, contentType: 'text/html; charset=utf-8', body: '<script>location.replace("/admin")</script>' })
      return
    }
    if (url.pathname === '/api/auth/logout' || url.pathname === '/api/auth/activity') {
      expect(route.request().headers()['x-csrf-token']).toBe(validSession().csrf_token)
      if (url.pathname === '/api/auth/logout') afterLogout?.()
      await route.fulfill({ contentType: 'application/json', body: JSON.stringify({ ok: true }) })
      return
    }
    if (url.pathname === '/api/admin/overview') return route.fulfill({ contentType: 'application/json', body: JSON.stringify(mockOverview()) })
    if (url.pathname === '/api/admin/providers') return route.fulfill({ contentType: 'application/json', body: JSON.stringify(mockProviders()) })
    if (url.pathname === '/api/admin/runs') return route.fulfill({ contentType: 'application/json', body: JSON.stringify(mockRuns()) })
    if (url.pathname === '/api/admin/runs/current-partial') return route.fulfill({ contentType: 'application/json', body: JSON.stringify(mockDetail()) })
    if (url.pathname === '/api/admin/coverage') return route.fulfill({ contentType: 'application/json', body: JSON.stringify(focusCoverage()) })
    await route.fulfill({ contentType: 'application/json', body: '{}' })
  })
}

test('admin sign-in uses fixed same-origin navigation and simulated provider callback', async ({ page, baseURL }) => {
  let authenticated = false
  await installApiStubs(page, () => authenticated ? 200 : 401, () => { authenticated = true })
  await page.context().addCookies([{
    name: 'xbray_oauth_flow_dev', value: 'mock-flow-cookie', url: baseURL ?? 'http://127.0.0.1:4173',
    httpOnly: true, sameSite: 'Lax',
  }])
  expect((await page.context().cookies(baseURL)).find(cookie => cookie.name === 'xbray_oauth_flow_dev')).toMatchObject({ httpOnly: true, sameSite: 'Lax' })
  await page.goto('/login?reason=login_failed')
  await expect(page.getByRole('heading', { name: 'Área administrativa' })).toBeVisible()
  await expect(page.getByText('Não foi possível concluir o acesso. Tente novamente.')).toBeVisible()
  await expect(page.getByRole('button', { name: 'Entrar com Google' })).toBeEnabled()
  await page.getByRole('button', { name: 'Entrar com Google' }).click()
  await expect(page).toHaveURL(/\/api\/auth\/login$/)
  await expect(page.getByRole('heading', { name: 'Provedor Google simulado' })).toBeVisible()
  await page.getByRole('button', { name: /Continuar/ }).click()
  const expectedHost = new URL(baseURL ?? 'http://127.0.0.1').host
  await expect(page).toHaveURL(new RegExp(`${expectedHost}/admin$`))
  await expect(page.getByRole('heading', { name: 'Estado registrado', exact: true })).toBeVisible()
  const cookie = await page.context().cookies()
  expect(cookie.find(item => item.name === 'xbray_admin_dev')?.httpOnly).toBe(true)
})

test('direct admin URL hides content until session validation and redirects a signed-out visitor', async ({ page }) => {
  await installApiStubs(page, () => 401)
  await page.goto('/admin')
  await expect(page).toHaveURL(/\/login\?reason=session_expired$/)
  await expect(page.getByRole('heading', { name: 'Área administrativa' })).toBeVisible()
  await expect(page.getByText('Estado registrado')).toHaveCount(0)
})

test('unavailable session keeps private content hidden and public pages do not query auth', async ({ page }) => {
  const authRequests: string[] = []
  page.on('request', request => {
    if (request.url().includes('/api/auth/')) authRequests.push(new URL(request.url()).pathname)
  })
  await installApiStubs(page, () => 503)
  await page.goto('/')
  await expect(page.getByText('Raio-X do Macro Brasileiro')).toBeVisible()
  expect(authRequests).toEqual([])
  await page.goto('/admin')
  await expect(page.getByRole('heading', { name: 'Área administrativa indisponível' })).toBeVisible()
  await expect(page.getByText('Estado registrado')).toHaveCount(0)
})

test('logout sends CSRF and clears the private route after confirmation', async ({ page }) => {
  let sessionStatus = 200
  await installApiStubs(page, () => sessionStatus, undefined, () => { sessionStatus = 401 })
  await page.goto('/admin')
  await expect(page.getByRole('heading', { name: 'Estado registrado', exact: true })).toBeVisible()
  await page.getByRole('button', { name: 'Sair' }).click()
  await expect(page).toHaveURL(/\/login\?reason=signed_out$/)
  await expect(page.getByRole('heading', { name: 'Estado registrado', exact: true })).toHaveCount(0)
})

test('login remains keyboard-operable and readable at a narrow viewport', async ({ page }) => {
  await page.setViewportSize({ width: 375, height: 812 })
  await installApiStubs(page, () => 401)
  await page.goto('/login')
  const loginButton = page.getByRole('button', { name: 'Entrar com Google' })
  await expect(loginButton).toBeVisible()
  await loginButton.focus()
  await expect(loginButton).toBeFocused()
  await page.keyboard.press('Enter')
  await expect(page).toHaveURL(/\/api\/auth\/login$/)
  await expect(page.getByRole('heading', { name: 'Provedor Google simulado' })).toBeVisible()
})

test('private monitoring routes show persisted run facts and honest unknown coverage without writes', async ({ page }) => {
  const methods: string[] = []
  page.on('request', request => { if (new URL(request.url()).pathname.startsWith('/api/admin/')) methods.push(request.method()) })
  await installApiStubs(page, () => 200)
  await page.goto('/admin')
  await expect(page.getByText('current-partial')).toBeVisible()
  await expect(page.getByText('prior-success')).toBeVisible()
  await page.getByRole('link', { name: 'Atualizações', exact: true }).click()
  await expect(page.getByRole('heading', { name: 'Execuções registradas' })).toBeVisible()
  await page.getByRole('link', { name: 'current-partial' }).click()
  await expect(page.getByRole('heading', { name: 'current-partial' })).toBeVisible()
  await expect(page.getByText(/frescura atual não está incluída/)).toBeVisible()
  await page.getByRole('link', { name: 'Cobertura e fontes' }).click()
  await page.getByLabel('Fonte').selectOption('focus')
  await expect(page.getByText('Cobertura ainda não avaliada: metadados/contrato insuficientes.')).toBeVisible()
  expect(methods.every(method => method === 'GET')).toBe(true)
})
