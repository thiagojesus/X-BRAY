import { expect, type Page, test } from '@playwright/test'

const activityPayload = {
  source: 'local fixture',
  data: {
    pib: [{ data: '01/01/2025', valor: '12000000000000' }],
    ibc_br: [{ data: '01/08/2026', valor: '153.42' }],
    desemprego: [{ data: '01/08/2026', valor: '6.5' }],
    resultado_primario: [{ data: '01/08/2026', valor: '45000' }],
  },
}

const completeQuality = {
  status: 'ok',
  last_updated: '2026-09-12',
  metadata: {
    ibc_br_sa: { freshness: 'ok' },
  },
  unavailable: [],
}

const sectorData = {
  ibc_br_sa: activityPayload.data.ibc_br,
  ibc_ex_agro_sa: [{ data: '01/08/2026', valor: '150' }],
  ibc_agro_sa: [{ data: '01/08/2026', valor: '160' }],
  ibc_industria_sa: [{ data: '01/08/2026', valor: '140' }],
  ibc_servicos_sa: [{ data: '01/08/2026', valor: '155' }],
  rendimento_medio_real: [{ data: '01/08/2026', valor: '3000' }],
}

async function mockCommonRoutes(page: Page, expectedRetry = false): Promise<string[]> {
  const failures: string[] = []
  page.on('response', response => {
    if (response.status() < 500) return
    if (expectedRetry && response.status() === 503 && new URL(response.url()).pathname === '/api/atividade') return
    failures.push(`${response.status()} ${response.url()}`)
  })
  page.on('requestfailed', request => {
    failures.push(`${request.url()} ${request.failure()?.errorText ?? ''}`)
  })
  await page.route('**/api/**', route => route.abort('blockedbyclient'))
  await page.route('**/api/status', route => route.fulfill({
    status: 200,
    contentType: 'application/json',
    body: JSON.stringify({ status: 'ok', last_updated: '2026-09-12T12:00:00Z' }),
  }))
  await page.route('**/api/atividade/sidra', route => route.fulfill({
    status: 200,
    contentType: 'application/json',
    body: JSON.stringify({ status: 'ok', data: {}, metadata: {}, unavailable: [] }),
  }))
  for (const mode of ['level', 'mom', 'yoy']) {
    await page.route(`**/api/atividade/setorial?mode=${mode}`, route => {
      return route.fulfill({
        status: 200,
        contentType: 'application/json',
        body: JSON.stringify({
          ...completeQuality,
          metadata: { ibc_br_sa: { freshness: expectedRetry ? 'stale' : 'ok' } },
          mode,
          data: mode === 'level' ? sectorData : {
            ibc_br_sa: [{ data: '01/08/2026', valor: mode === 'mom' ? '1.2' : '3.4' }],
          },
        }),
      })
    })
  }
  return failures
}

test('data quality happy: renders an explicit partial-data notice without hiding valid series', async ({ page }) => {
  const failures = await mockCommonRoutes(page)
  await page.route('**/api/atividade', route => route.fulfill({
    status: 200,
    contentType: 'application/json',
    body: JSON.stringify(activityPayload),
  }))
  await page.route('**/api/atividade/setorial?mode=level', route => route.fulfill({
    status: 200,
    contentType: 'application/json',
    body: JSON.stringify({
      ...completeQuality,
      mode: 'level',
      status: 'partial',
      data: { ...sectorData, ibc_ex_agro_sa: [] },
      unavailable: ['ibc_ex_agro_sa'],
    }),
  }))

  await page.goto('/atividade')

  await expect(page.getByRole('status')).toContainText('Fonte parcialmente indisponível.')
  await expect(page.getByText('Séries indisponíveis neste modo: IBC-Br ex-agro.')).toBeVisible()
  await expect(page.locator('.kpi-card').filter({ hasText: 'IBC-Br m/m' })).toBeVisible()
  await expect(page.getByText('1,2%', { exact: true })).toBeVisible()
  await expect(page.getByText('6,5%', { exact: true })).toBeVisible()
  await expect(page.locator('.chart-container')).toHaveCount(3)
  await expect(page.getByRole('heading', { name: 'Aprofundamento IBGE/SIDRA' })).toBeVisible()
  expect(failures).toEqual([])
})

test('handles failure states: keeps cached-stale data visible when an online retry fails', async ({ page }) => {
  const failures = await mockCommonRoutes(page, true)
  let activityRequests = 0
  let retryStarted = false
  await page.route('**/api/atividade', route => {
    activityRequests += 1
    if (!retryStarted) {
      return route.fulfill({
        status: 200,
        contentType: 'application/json',
        body: JSON.stringify(activityPayload),
      })
    }
    return route.fulfill({ status: 503, contentType: 'application/json', body: '{"detail":"offline"}' })
  })
  await page.goto('/atividade')
  await expect(page.getByText('1,2%', { exact: true })).toBeVisible()
  await expect(page.getByText('6,5%', { exact: true })).toBeVisible()
  await expect(page.getByRole('heading', { name: 'Aprofundamento IBGE/SIDRA' })).toBeVisible()
  await expect(page.getByRole('status')).toContainText('Dados possivelmente desatualizados.')

  const initialRequests = activityRequests
  retryStarted = true
  const retryResponse = page.waitForResponse(response =>
    new URL(response.url()).pathname === '/api/atividade' && response.status() === 503,
  )
  await page.evaluate(() => window.dispatchEvent(new Event('online')))
  await retryResponse

  await expect.poll(() => activityRequests).toBeGreaterThan(initialRequests)
  await expect(page.getByRole('status')).toContainText('Dados possivelmente desatualizados.')
  await expect(page.getByRole('status')).toContainText('Exibindo a última atualização disponível.')
  await expect(page.getByText('1,2%', { exact: true })).toBeVisible()
  await expect(page.getByText('3,4%', { exact: true })).toBeVisible()
  await expect(page.getByText('6,5%', { exact: true })).toBeVisible()
  await expect(page.locator('.chart-container')).toHaveCount(3)
  expect(failures).toEqual([])
})
