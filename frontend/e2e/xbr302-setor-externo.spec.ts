import { readFileSync } from 'node:fs'
import { mkdir } from 'node:fs/promises'
import path from 'node:path'

import { expect, type Page, type Route, test } from '@playwright/test'

type SeriesPoint = {
  readonly data: string
  readonly valor: string
}

type ExternalContract = {
  readonly source: string
  readonly last_updated: string
  readonly status: 'ok' | 'partial'
  readonly data: Readonly<Record<string, readonly SeriesPoint[]>>
  readonly metadata: Readonly<Record<string, Readonly<Record<string, string | number>>>>
  readonly unavailable: readonly string[]
}

const evidenceDir = process.env.XBR302_SCREENSHOT_DIR
  ?? path.join(process.cwd(), 'test-results', 'evidence', 'xbr302')
const fixturePath = path.resolve(process.cwd(), '../backend/tests/fixtures/contracts/setor_externo.json')
const contract: ExternalContract = JSON.parse(readFileSync(fixturePath, 'utf8'))

const evidenceData = Object.fromEntries(
  Object.entries(contract.data).map(([key, points]) => {
    const latest = points.at(-1)
    if (!latest) return [key, points]
    const value = Number(latest.valor)
    return [key, [
      { data: '01/05/2026', valor: String(value * 0.92) },
      { data: '01/06/2026', valor: String(value * 0.96) },
      ...points,
    ]]
  }),
)

const externalPayload = {
  ...contract,
  data: evidenceData,
  last_updated: '2026-09-21T12:00:00Z',
}

const complementaryPayload = {
  data: {
    reservas_internacionais: [{ data: '01/08/2026', valor: '372616.0' }],
    ic_commodities: [{ data: '01/08/2026', valor: '120.5' }],
    m0: [{ data: '01/08/2026', valor: '432500' }],
    m1: [{ data: '01/08/2026', valor: '647800' }],
    m2: [{ data: '01/08/2026', valor: '7650000' }],
  },
}

async function mockShell(page: Page): Promise<void> {
  await page.route('**/*', route => {
    const hostname = new URL(route.request().url()).hostname
    return hostname === '127.0.0.1' ? route.continue() : route.abort('blockedbyclient')
  })
  await page.route('**/api/status', route => route.fulfill({
    status: 200,
    contentType: 'application/json',
    body: JSON.stringify({ status: 'running', db_stats: {} }),
  }))
  await page.route('**/api/complementares', route => route.fulfill({
    status: 200,
    contentType: 'application/json',
    body: JSON.stringify(complementaryPayload),
  }))
  await page.route('**/api/data-quality/complementares', route => route.fulfill({
    status: 200,
    contentType: 'application/json',
    body: JSON.stringify({
      status: 'ok',
      last_updated: '2026-09-21T12:00:00Z',
      unavailable: [],
      metadata: {},
    }),
  }))
}

async function mockExternal(page: Page, handler: (route: Route) => Promise<void> | void): Promise<void> {
  await page.route('**/api/setor-externo', handler)
}

function kpiCard(page: Page, name: string) {
  return page.locator('.kpi-card').filter({
    has: page.getByRole('button', { name: `Informações sobre ${name}` }),
  })
}

async function expectNoOverflow(page: Page): Promise<void> {
  const widths = await page.evaluate(() => ({
    inner: window.innerWidth,
    document: document.documentElement.scrollWidth,
    body: document.body.scrollWidth,
  }))
  expect(widths.document).toBeLessThanOrEqual(widths.inner)
  expect(widths.body).toBeLessThanOrEqual(widths.inner)
}

for (const viewport of [
  { width: 375, height: 812 },
  { width: 768, height: 1024 },
  { width: 1280, height: 900 },
]) {
  for (const colorScheme of ['light', 'dark'] as const) {
    test(`Setor Externo happy navigation ${viewport.width} ${colorScheme}`, async ({ page }, testInfo) => {
      const consoleErrors: string[] = []
      const requestFailures: string[] = []
      page.on('console', message => {
        if (message.type() === 'error') consoleErrors.push(message.text())
      })
      page.on('requestfailed', request => requestFailures.push(`${request.url()}: ${request.failure()?.errorText ?? 'unknown'}`))
      await page.setViewportSize(viewport)
      await page.emulateMedia({ colorScheme })
      await mockShell(page)
      await mockExternal(page, route => route.fulfill({
        status: 200,
        contentType: 'application/json',
        body: JSON.stringify(externalPayload),
      }))

      await page.goto('/')
      await page.getByRole('link', { name: /Setor Externo/ }).last().click()

      await expect(page).toHaveURL(/\/setor-externo$/)
      await expect(page.locator('header.main-header')).toContainText('Setor Externo')
      await expect(page.locator('.sidebar-nav a[href="/setor-externo"]')).toHaveAttribute('aria-current', 'page')
      await expect(kpiCard(page, 'Conta corrente 12m / PIB').locator('.kpi-value')).toHaveText('-2,5% do PIB')
      await expect(kpiCard(page, 'IDP 12m / PIB').locator('.kpi-value')).toHaveText('3,5% do PIB')
      await expect(kpiCard(page, 'Balança comercial').locator('.kpi-value')).toHaveText('US$ 6.152,2 mi')
      await expect(kpiCard(page, 'Investimento em carteira').locator('.kpi-value')).toHaveText('US$ -221,1 mi')
      await expect(kpiCard(page, 'Reservas internacionais').locator('.kpi-value')).toHaveText('US$ 372.616,0 mi')
      await expect(page.getByText(/1,41×.*coberto/i)).toBeVisible()
      await expect(page.getByText(/PTAX permanece em Câmbio/i)).toBeVisible()
      await expect(page.locator('.chart-container')).toHaveCount(4)
      await expect(page.locator('.recharts-line-curve')).toHaveCount(6)
      await expectNoOverflow(page)

      await page.goto('/complementares')
      await expect(page.locator('.kpi-card')).toHaveCount(4)
      await expect(page.locator('body')).not.toContainText('Reservas (USD mi)')
      const reservesLink = page.getByRole('link', { name: 'Reservas agora em Setor Externo' })
      await reservesLink.focus()
      await page.keyboard.press('Enter')
      await expect(page).toHaveURL(/\/setor-externo$/)

      if (viewport.width <= 1024) {
        const menu = page.locator('button.menu-toggle')
        await menu.focus()
        await page.keyboard.press('Enter')
        await expect(menu).toHaveAttribute('aria-expanded', 'true')
        const close = page.locator('button.sidebar-close')
        await close.focus()
        await page.keyboard.press('Enter')
        await expect(menu).toHaveAttribute('aria-expanded', 'false')
      }

      if (testInfo.project.name === 'chromium') {
        await page.mouse.move(viewport.width - 4, 4)
        await page.waitForTimeout(300)
        await mkdir(evidenceDir, { recursive: true })
        await page.screenshot({
          path: path.join(evidenceDir, `task-9-setor-externo-${viewport.width}-${colorScheme}.png`),
          fullPage: true,
        })
      }
      expect(consoleErrors).toEqual([])
      expect(requestFailures).toEqual([])
    })
  }
}

test('Setor Externo coverage failure preserves partial charts without a false claim', async ({ page }) => {
  await mockShell(page)
  await mockExternal(page, route => route.fulfill({
    status: 200,
    contentType: 'application/json',
    body: JSON.stringify({
      ...externalPayload,
      status: 'partial',
      unavailable: ['idp_12m_pib'],
      data: { ...externalPayload.data, idp_12m_pib: [] },
    }),
  }))

  await page.goto('/setor-externo')

  await expect(page.getByRole('status')).toContainText('Fonte parcialmente indisponível.')
  await expect(kpiCard(page, 'Conta corrente 12m / PIB').locator('.kpi-value')).toHaveText('-2,5% do PIB')
  await expect(kpiCard(page, 'IDP 12m / PIB').locator('.kpi-value')).toHaveText('Indisponível')
  await expect(page.getByText('Cobertura por IDP indisponível')).toBeVisible()
  await expect(page.locator('.chart-container')).toHaveCount(4)
  await expect(page.locator('body')).not.toContainText('NaN')
  await expectNoOverflow(page)
})

test('Setor Externo coverage failure rejects surplus and zero conclusions', async ({ page }) => {
  await mockShell(page)
  let currentAccount = '2.49'
  await mockExternal(page, route => route.fulfill({
    status: 200,
    contentType: 'application/json',
    body: JSON.stringify({
      ...externalPayload,
      data: {
        ...externalPayload.data,
        conta_corrente_12m_pib: [{ data: '01/07/2026', valor: currentAccount }],
      },
    }),
  }))

  await page.goto('/setor-externo')
  await expect(page.getByText('Cobertura por IDP indisponível')).toBeVisible()

  currentAccount = '0'
  await page.reload()
  await expect(page.getByText('Cobertura por IDP indisponível')).toBeVisible()
  await expect(page.locator('body')).not.toContainText('não coberto')
})
