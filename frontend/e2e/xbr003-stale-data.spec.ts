import { expect, test, type Page } from '@playwright/test'

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
  dataset: 'atividade',
  status: 'ok',
  last_updated: '2026-09-12',
  metadata: {
    pib: { freshness: 'ok' },
    ibc_br: { freshness: 'ok' },
    desemprego: { freshness: 'ok' },
    resultado_primario: { freshness: 'ok' },
  },
  unavailable: [],
}

async function mockCommonRoutes(page: Page): Promise<void> {
  await page.route('**/api/status', route => route.fulfill({
    status: 200,
    contentType: 'application/json',
    body: JSON.stringify({ status: 'ok', last_updated: '2026-09-12T12:00:00Z' }),
  }))
}

test('data quality happy: renders an explicit partial-data notice without hiding valid series', async ({ page }) => {
  await mockCommonRoutes(page)
  await page.route('**/api/atividade', route => route.fulfill({
    status: 200,
    contentType: 'application/json',
    body: JSON.stringify(activityPayload),
  }))
  await page.route('**/api/data-quality/atividade', route => route.fulfill({
    status: 200,
    contentType: 'application/json',
    body: JSON.stringify({
      ...completeQuality,
      status: 'partial',
      unavailable: [{ series: 'desemprego', required: true, reason: 'provider_unavailable' }],
    }),
  }))

  await page.goto('/atividade')

  await expect(page.getByRole('status')).toContainText('Fonte parcialmente indisponível.')
  await expect(page.getByText('IBC-Br (índice)', { exact: true }).first()).toBeVisible()
  await expect(page.getByText('153,4')).toBeVisible()
})

test('handles failure states: keeps last good data visible when an online retry fails', async ({ page }) => {
  await mockCommonRoutes(page)
  let activityRequests = 0
  await page.route('**/api/atividade', route => {
    activityRequests += 1
    if (activityRequests === 1) {
      return route.fulfill({
        status: 200,
        contentType: 'application/json',
        body: JSON.stringify(activityPayload),
      })
    }
    return route.fulfill({ status: 503, contentType: 'application/json', body: '{"detail":"offline"}' })
  })
  await page.route('**/api/data-quality/atividade', route => route.fulfill({
    status: 200,
    contentType: 'application/json',
    body: JSON.stringify(completeQuality),
  }))
  await page.goto('/atividade')
  await expect(page.getByText('153,4')).toBeVisible()

  await page.evaluate(() => window.dispatchEvent(new Event('online')))

  await expect(page.getByRole('status')).toContainText('Fonte indisponível.')
  await expect(page.getByText('153,4')).toBeVisible()
  await expect(page.getByText('6.5%')).toBeVisible()
})
