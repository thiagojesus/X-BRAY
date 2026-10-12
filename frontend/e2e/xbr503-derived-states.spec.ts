import { expect, test } from '@playwright/test'
import {
  aggregateFixture,
  captureChromiumScreenshot,
  expectNoOverflow,
  mockShell,
  realRateFixture,
} from './xbr503-fixtures'

test('Derivados shows aggregate loading without fabricated cards', async ({ page }, testInfo) => {
  // Given
  await page.setViewportSize({ width: 1280, height: 900 })
  await mockShell(page)
  let releaseResponse: (() => void) | null = null
  await page.route('**/api/derivados', async route => {
    await new Promise<void>(resolve => { releaseResponse = resolve })
    await route.fulfill({ status: 200, contentType: 'application/json', body: JSON.stringify(aggregateFixture) })
  })
  await page.route('**/api/derivados/juro-real-ex-ante-1y', route => route.fulfill({
    status: 200,
    contentType: 'application/json',
    body: JSON.stringify(realRateFixture),
  }))

  // When
  await page.goto('/derivados')

  // Then
  const section = page.getByRole('region', { name: 'Outros indicadores derivados' })
  await expect(section.getByText('Carregando indicadores adicionais...')).toBeVisible()
  await expect(page.getByText('6,84% a.a.').first()).toBeVisible()
  await expect(section.getByRole('article')).toHaveCount(0)
  await expect.poll(() => releaseResponse !== null).toBe(true)
  await section.scrollIntoViewIfNeeded()
  await captureChromiumScreenshot(page, testInfo, 'derivados-1280-loading.png')
  if (releaseResponse !== null) releaseResponse()
  await expect(section.getByText('140,00%')).toBeVisible()
})

test('Derivados identifies a missing snapshot beside available peers', async ({ page }, testInfo) => {
  // Given
  await page.setViewportSize({ width: 375, height: 812 })
  await mockShell(page)
  const { spread_credito_pf_selic: _missing, ...data } = aggregateFixture.data
  await page.route('**/api/derivados', route => route.fulfill({
    status: 200,
    contentType: 'application/json',
    body: JSON.stringify({ status: 'ok', data }),
  }))
  await page.route('**/api/derivados/juro-real-ex-ante-1y', route => route.fulfill({
    status: 200,
    contentType: 'application/json',
    body: JSON.stringify(realRateFixture),
  }))

  // When
  await page.goto('/derivados')

  // Then
  const section = page.getByRole('region', { name: 'Outros indicadores derivados' })
  const missingCard = section.getByRole('article', { name: 'Spread crédito PF × Selic' })
  await expect(missingCard.getByText('Snapshot não encontrado neste cache.')).toBeVisible()
  await expect(section.getByText('140,00%')).toBeVisible()
  await expectNoOverflow(page)
  await section.scrollIntoViewIfNeeded()
  await captureChromiumScreenshot(page, testInfo, 'derivados-375-missing-snapshot.png')
})

test('Derivados isolates a malformed snapshot from valid peers', async ({ page }, testInfo) => {
  // Given
  await page.setViewportSize({ width: 768, height: 1024 })
  await mockShell(page)
  await page.route('**/api/derivados', route => route.fulfill({
    status: 200,
    contentType: 'application/json',
    body: JSON.stringify({
      ...aggregateFixture,
      data: {
        ...aggregateFixture.data,
        cobertura_externa: { ...aggregateFixture.data.cobertura_externa, value: Number.NaN },
      },
    }),
  }))
  await page.route('**/api/derivados/juro-real-ex-ante-1y', route => route.fulfill({
    status: 200,
    contentType: 'application/json',
    body: JSON.stringify(realRateFixture),
  }))

  // When
  await page.goto('/derivados')

  // Then
  const section = page.getByRole('region', { name: 'Outros indicadores derivados' })
  const coverage = section.getByRole('article', { name: 'Cobertura externa por IDP' })
  await expect(coverage.getByText('Payload inválido.')).toBeVisible()
  await expect(section.getByText('10,12 p.p.')).toBeVisible()
  await expectNoOverflow(page)
  await section.scrollIntoViewIfNeeded()
  await captureChromiumScreenshot(page, testInfo, 'derivados-768-invalid-snapshot.png')
})

test('Derivados retains cached cards and marks them stale after retry failure', async ({ page }, testInfo) => {
  // Given
  await page.setViewportSize({ width: 1280, height: 900 })
  await mockShell(page)
  let aggregateFailed = false
  await page.route('**/api/derivados', route => aggregateFailed
    ? route.fulfill({ status: 503, body: 'unavailable' })
    : route.fulfill({ status: 200, contentType: 'application/json', body: JSON.stringify(aggregateFixture) }))
  await page.route('**/api/derivados/juro-real-ex-ante-1y', route => route.fulfill({
    status: 200,
    contentType: 'application/json',
    body: JSON.stringify(realRateFixture),
  }))

  // When
  await page.goto('/derivados')

  // Then
  const section = page.getByRole('region', { name: 'Outros indicadores derivados' })
  await expect(section.getByText('140,00%')).toBeVisible()
  aggregateFailed = true
  await page.evaluate(() => window.dispatchEvent(new Event('online')))
  await expect(section.getByRole('alert')).toContainText('HTTP 503')
  await expect(section.getByText('Dados possivelmente desatualizados.')).toBeVisible()
  await expect(section.getByText('140,00%')).toBeVisible()
  await expectNoOverflow(page)
  await section.scrollIntoViewIfNeeded()
  await captureChromiumScreenshot(page, testInfo, 'derivados-1280-stale.png')
})
