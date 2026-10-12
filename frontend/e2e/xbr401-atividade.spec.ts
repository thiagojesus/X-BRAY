import { readFileSync } from 'node:fs'
import { mkdir } from 'node:fs/promises'
import path from 'node:path'

import { expect, type Page, test } from '@playwright/test'

type Point = { readonly data: string; readonly valor: string }
type SeriesData = Readonly<Record<string, readonly Point[]>>

const evidenceDir = process.env.XBR401_SCREENSHOT_DIR
  ?? '/Users/thiagojesus/.omo/evidence/xbray-roadmap-harness'
const fixturePath = path.resolve(process.cwd(), '../backend/tests/fixtures/activity_sgs.json')
const fixture: SeriesData = JSON.parse(readFileSync(fixturePath, 'utf8'))

function variation(points: readonly Point[], lag: number): Point[] {
  return points.flatMap((point, index) => {
    if (index < lag) return []
    const current = Number(point.valor)
    const previous = Number(points[index - lag].valor)
    return [{ data: point.data, valor: String(((current / previous) - 1) * 100) }]
  })
}

const payloads = {
  level: fixture,
  mom: Object.fromEntries(Object.entries(fixture).map(([key, points]) => [key, variation(points, 1)])),
  yoy: Object.fromEntries(Object.entries(fixture).map(([key, points]) => [key, variation(points, 2)])),
}

const quality = {
  source: 'Banco Central do Brasil - SGS',
  last_updated: '2026-09-26T12:00:00Z',
  status: 'ok',
  unavailable: [],
  metadata: {},
}

async function mockActivity(page: Page): Promise<void> {
  await page.route('**/*', route => {
    const hostname = new URL(route.request().url()).hostname
    return hostname === '127.0.0.1' ? route.continue() : route.abort('blockedbyclient')
  })
  await page.route('**/api/**', route => route.abort('blockedbyclient'))
  await page.route('**/api/status', route => route.fulfill({
    status: 200,
    contentType: 'application/json',
    body: JSON.stringify({ status: 'running', db_stats: {} }),
  }))
  await page.route('**/api/atividade/setorial?mode=*', route => {
    const mode = new URL(route.request().url()).searchParams.get('mode')
    if (mode !== 'level' && mode !== 'mom' && mode !== 'yoy') return route.abort()
    return route.fulfill({
      status: 200,
      contentType: 'application/json',
      body: JSON.stringify({ ...quality, mode, data: payloads[mode] }),
    })
  })
  await page.route('**/api/atividade', route => route.fulfill({
    status: 200,
    contentType: 'application/json',
    body: JSON.stringify({
      data: {
        ibc_br: fixture.ibc_br_sa,
        desemprego: [
          { data: '01/12/2024', valor: '6.8' },
          { data: '01/01/2025', valor: '6.6' },
          { data: '01/01/2026', valor: '6.5' },
        ],
      },
    }),
  }))
  await page.route('**/api/atividade/sidra', route => route.fulfill({
    status: 200,
    contentType: 'application/json',
    body: JSON.stringify({ status: 'ok', data: {}, metadata: {}, unavailable: [] }),
  }))
}

async function expectNoOverflow(page: Page): Promise<void> {
  const widths = await page.evaluate(() => ({
    viewport: window.innerWidth,
    document: document.documentElement.scrollWidth,
    body: document.body.scrollWidth,
  }))
  expect(widths.document).toBeLessThanOrEqual(widths.viewport)
  expect(widths.body).toBeLessThanOrEqual(widths.viewport)
}

for (const viewport of [
  { width: 375, height: 812 },
  { width: 768, height: 1024 },
  { width: 1280, height: 900 },
]) {
  test(`Atividade fixture-only modes ${viewport.width}`, async ({ page }, testInfo) => {
    const consoleErrors: string[] = []
    const failedRequests: string[] = []
    const serverErrors: string[] = []
    page.on('requestfailed', request => failedRequests.push(request.url()))
    page.on('response', response => {
      if (response.status() >= 500) serverErrors.push(`${response.status()} ${response.url()}`)
    })
    page.on('console', message => {
      if (message.type() === 'error') consoleErrors.push(message.text())
    })
    await page.setViewportSize(viewport)
    await mockActivity(page)
    await page.goto('/atividade')

    await expect(page.locator('.kpi-card')).toHaveCount(4)
    await expect(page.locator('.chart-container')).toHaveCount(3)
    await expect(page.getByRole('heading', { name: 'Aprofundamento IBGE/SIDRA' })).toBeVisible()
    await expect(page.getByRole('button', { name: 'Nível' })).toHaveAttribute('aria-pressed', 'true')
    await expect(page.locator('.activity-mode-unit')).toHaveText('Índice (base 100 na janela)')

    for (const mode of [
      { name: 'Nível', unit: 'Índice (base 100 na janela)', slug: 'level' },
      { name: 'Variação mensal', unit: 'Variação mensal (%)', slug: 'mom' },
      { name: 'Variação 12m', unit: 'Variação 12m (%)', slug: 'yoy' },
    ]) {
      const button = page.getByRole('button', { name: mode.name, exact: true })
      await button.click()
      await expect(button).toHaveAttribute('aria-pressed', 'true')
      await expect(page.locator('.activity-mode-unit')).toHaveText(mode.unit)
      await expectNoOverflow(page)
      await expect(page.locator('body')).not.toContainText(/NaN|Infinity/)
      if (testInfo.project.name === 'chromium') {
        await mkdir(evidenceDir, { recursive: true })
        await page.screenshot({
          path: path.join(evidenceDir, `task-11-atividade-${viewport.width}-${mode.slug}.png`),
          fullPage: true,
        })
      }
    }
    expect(consoleErrors).toEqual([])
    expect(failedRequests).toEqual([])
    expect(serverErrors).toEqual([])
  })
}
