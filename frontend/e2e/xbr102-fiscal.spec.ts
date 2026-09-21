import { mkdir } from 'node:fs/promises'
import path from 'node:path'
import { expect, test, type Page, type Route } from '@playwright/test'

const evidenceDir = process.env.XBR102_SCREENSHOT_DIR
  ?? '/Users/thiagojesus/.omo/evidence/xbray-roadmap-harness'

const fiscalPayload = {
  source: 'BCB SGS',
  last_updated: '2026-09-01T12:00:00Z',
  status: 'ok',
  unavailable: [],
  data: {
    dbgg_pib: [{ data: '01/07/2026', valor: '82.56' }],
    dlsp_pib: [{ data: '01/07/2026', valor: '69.13' }],
    saldo_primario_12m_pib: [{ data: '01/07/2026', valor: '1.20' }],
    juros_nominais_12m_pib: [{ data: '01/07/2026', valor: '6.00' }],
    saldo_nominal_12m_pib: [{ data: '01/07/2026', valor: '-2.50' }],
  },
  metadata: {
    dbgg_pib: { code: 13762, unit: 'pct_gdp', frequency: 'monthly', freshness: 'ok', sign_convention: 'debt_positive' },
    dlsp_pib: { code: 4513, unit: 'pct_gdp', frequency: 'monthly', freshness: 'ok', sign_convention: 'debt_positive' },
    saldo_primario_12m_pib: { code: 5793, unit: 'pct_gdp', frequency: 'monthly', freshness: 'ok', sign_convention: 'surplus_positive' },
    juros_nominais_12m_pib: { code: 5760, unit: 'pct_gdp', frequency: 'monthly', freshness: 'ok', sign_convention: 'cost_positive' },
    saldo_nominal_12m_pib: { code: 5727, unit: 'pct_gdp', frequency: 'monthly', freshness: 'ok', sign_convention: 'surplus_positive' },
  },
}

async function mockShell(page: Page): Promise<void> {
  await page.route('**/*', route => {
    const url = new URL(route.request().url())
    if (url.hostname === '127.0.0.1') return route.continue()
    return route.abort('blockedbyclient')
  })
  await page.route('**/api/status', route => route.fulfill({
    status: 200,
    contentType: 'application/json',
    body: JSON.stringify({ status: 'running', db_stats: {}, last_updated: '2026-09-01T12:00:00Z' }),
  }))
}

async function mockFiscal(page: Page, handler: (route: Route) => Promise<void> | void): Promise<void> {
  await page.route('**/api/fiscal', handler)
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
  test(`Fiscal happy responsive ${viewport.width}`, async ({ page }, testInfo) => {
    const consoleErrors: string[] = []
    const requestFailures: string[] = []
    page.on('console', message => {
      if (message.type() === 'error') consoleErrors.push(message.text())
    })
    page.on('requestfailed', request => requestFailures.push(`${request.url()}: ${request.failure()?.errorText ?? 'unknown'}`))
    await page.setViewportSize(viewport)
    await mockShell(page)
    await mockFiscal(page, route => route.fulfill({
      status: 200,
      contentType: 'application/json',
      body: JSON.stringify(fiscalPayload),
    }))

    await page.goto('/')
    await page.getByRole('link', { name: /Fiscal/ }).last().click()

    await expect(page).toHaveURL(/\/fiscal$/)
    await expect(page.locator('header.main-header')).toContainText('Fiscal')
    await expect(page.locator('.sidebar-nav a[href="/fiscal"]')).toHaveAttribute('aria-current', 'page')
    await expect(page.getByText('DBGG (% do PIB)', { exact: true }).first()).toBeVisible()
    await expect(page.getByText('DLSP (% do PIB)', { exact: true }).first()).toBeVisible()
    await expect(page.getByText('Superávit · +1,2% do PIB')).toBeVisible()
    await expect(page.getByText('Custo · 6,0% do PIB')).toBeVisible()
    await expect(page.getByText('Déficit · −2,5% do PIB')).toBeVisible()
    await expect(page.getByText('DBGG × DLSP')).toBeVisible()
    await expect(page.getByText('Saldos primário × nominal')).toBeVisible()
    await expect(page.getByText('Juros nominais', { exact: true }).last()).toBeVisible()
    await expect(page.getByText(/Convenção NFSP/)).toBeVisible()
    await expectNoOverflow(page)

    if (viewport.width <= 1024) {
      const menu = page.locator('button.menu-toggle')
      await expect(menu).toHaveAccessibleName('Abrir menu')
      await menu.click()
      const close = page.locator('button.sidebar-close')
      await expect(close).toBeVisible()
      await expect(page.getByRole('link', { name: 'Fiscal', exact: true })).toBeVisible()
      await close.focus()
      await page.keyboard.press('Enter')
      await expect(menu).toHaveAttribute('aria-expanded', 'false')
      await expect(menu).toHaveAccessibleName('Abrir menu')
      await menu.focus()
      await page.keyboard.press('Enter')
      await expect(menu).toHaveAttribute('aria-expanded', 'true')
      await expect(menu).toHaveAccessibleName('Fechar menu')
      await close.click()
      await expect(menu).toHaveAttribute('aria-expanded', 'false')
      await expect.poll(async () => {
        const box = await page.locator('aside.sidebar').boundingBox()
        return box === null || box.x + box.width <= 0
      }).toBe(true)
    }

    if (testInfo.project.name === 'chromium') {
      await page.mouse.move(viewport.width - 4, 4)
      await page.waitForTimeout(300)
      await mkdir(evidenceDir, { recursive: true })
      await page.screenshot({
        path: path.join(evidenceDir, `task-5-fiscal-${viewport.width}.png`),
        fullPage: true,
      })
    }
    expect(consoleErrors).toEqual([])
    expect(requestFailures).toEqual([])
  })
}

test('Fiscal failure states preserve partial and stale data', async ({ page }) => {
  await mockShell(page)
  await mockFiscal(page, route => route.fulfill({
    status: 200,
    contentType: 'application/json',
    body: JSON.stringify({
      ...fiscalPayload,
      status: 'partial',
      unavailable: ['saldo_nominal_12m_pib'],
      data: {
        ...fiscalPayload.data,
        saldo_nominal_12m_pib: undefined,
      },
      metadata: {
        ...fiscalPayload.metadata,
        dbgg_pib: { ...fiscalPayload.metadata.dbgg_pib, freshness: 'stale' },
      },
    }),
  }))

  await page.goto('/fiscal')

  await expect(page.getByRole('status')).toContainText('Fonte parcialmente indisponível.')
  await expect(page.getByText('82,6% do PIB')).toBeVisible()
  await expect(page.getByText('Saldo nominal 12m', { exact: true })).toBeVisible()
  await expectNoOverflow(page)
})

test('Fiscal failure states report HTTP 503 and network failures', async ({ page }) => {
  await mockShell(page)
  let requestCount = 0
  await mockFiscal(page, route => {
    requestCount += 1
    if (requestCount === 1) {
      return route.fulfill({ status: 503, contentType: 'application/json', body: '{"error":"dataset_unavailable","dataset":"fiscal"}' })
    }
    return route.abort('failed')
  })

  await page.goto('/fiscal')
  await expect(page.getByRole('alert')).toContainText('HTTP 503')

  await page.reload()
  await expect(page.getByRole('alert')).toContainText('Dados indisponíveis.')
})
