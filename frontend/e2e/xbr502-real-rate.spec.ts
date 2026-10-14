import { readFileSync } from 'node:fs'
import { mkdir } from 'node:fs/promises'
import path from 'node:path'

import { expect, type Page, type Route, test } from '@playwright/test'

const evidenceDir = process.env.XBR502_SCREENSHOT_DIR
  ?? path.join(process.cwd(), 'test-results', 'evidence', 'xbr502')
const fixturePath = path.resolve(process.cwd(), '../backend/tests/fixtures/contracts/juro_real_ex_ante_1y.json')
const realRatePayload = JSON.parse(readFileSync(fixturePath, 'utf8'))
const neutralDisclosure = 'O resultado é uma medida derivada dos dados disponíveis no corte; não é uma previsão nem uma afirmação causal.'

async function mockShell(page: Page): Promise<void> {
  await page.route('**/*', route => {
    const hostname = new URL(route.request().url()).hostname
    return hostname === '127.0.0.1' ? route.continue() : route.abort('blockedbyclient')
  })
  await page.route('**/api/status', route => route.fulfill({
    status: 200,
    contentType: 'application/json',
    body: JSON.stringify({ status: 'running', db_stats: {}, last_updated: '2026-09-27T12:00:00Z' }),
  }))
  await page.route('**/api/derivados', route => route.fulfill({
    status: 200,
    contentType: 'application/json',
    body: JSON.stringify({ status: 'unavailable', data: {} }),
  }))
  await page.route('**/api/derivados/history', route => route.fulfill({
    status: 200,
    contentType: 'application/json',
    body: JSON.stringify({
      status: 'unavailable',
      window: {
        start_month: '2024-11',
        end_month: '2026-10',
        cutoff: '2026-10-14T14:00:00Z',
      },
      data: {
        juro_real_ex_ante_1y: [],
        cobertura_externa: [],
        variacao_saldo_primario_12m: [],
        spread_credito_pf_selic: [],
        distancia_focus_meta: [],
        inclinacao_di_5y_1y: [],
      },
    }),
  }))
}

async function mockRealRate(page: Page, handler: (route: Route) => Promise<void> | void): Promise<void> {
  await page.route('**/api/derivados/juro-real-ex-ante-1y', handler)
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
  test(`Juro real happy responsive ${viewport.width}`, async ({ page }, testInfo) => {
    const consoleErrors: string[] = []
    const requestFailures: string[] = []
    page.on('console', message => {
      if (message.type() === 'error') consoleErrors.push(message.text())
    })
    page.on('requestfailed', request => requestFailures.push(`${request.url()}: ${request.failure()?.errorText ?? 'unknown'}`))
    await page.setViewportSize(viewport)
    await mockShell(page)
    await mockRealRate(page, route => route.fulfill({
      status: 200,
      contentType: 'application/json',
      body: JSON.stringify(realRatePayload),
    }))

    await page.goto('/')
    await page.getByRole('link', { name: /Indicadores Derivados/ }).last().click()

    await expect(page).toHaveURL(/\/derivados$/)
    await expect(page.locator('header.main-header')).toContainText('Indicadores Derivados')
    await expect(page.locator('.sidebar-nav a[href="/derivados"]')).toHaveAttribute('aria-current', 'page')
    await expect(page.getByText('6,84% a.a.').first()).toBeVisible()
    await expect(page.getByText('Classificação em formação.')).toBeVisible()
    await expect(page.getByText('DI1V27')).toBeVisible()
    await expect(page.getByText('Focus IPCA 12m')).toBeVisible()

    const nominalRateCard = page.getByRole('article').filter({
      has: page.getByRole('heading', { name: 'Taxa nominal DI' }),
    })
    const expectedInflationCard = page.getByRole('article').filter({
      has: page.getByRole('heading', { name: 'Expectativa IPCA 12 meses' }),
    })
    await expect(page.getByRole('region', { name: 'Juro real ex-ante de um ano' }).getByText('Medida derivada', { exact: true })).toBeVisible()
    await expect(expectedInflationCard.getByText("Indicador eq 'IPCA' and Suavizada eq 'S' and baseCalculo eq 0", { exact: true })).toBeVisible()
    await expect(page.getByText('first_seen_at', { exact: true })).toHaveCount(2)
    await expect(nominalRateCard.getByText('first_seen_at', { exact: true })).toHaveCount(1)
    await expect(expectedInflationCard.getByText('first_seen_at', { exact: true })).toHaveCount(1)

    await expect(page.locator('details.derived-methodology')).not.toHaveAttribute('open')
    await expect(page.getByText(neutralDisclosure, { exact: true })).toBeVisible()
    const disclosure = page.getByText('Como o indicador é calculado')
    await disclosure.focus()
    await page.keyboard.press('Enter')
    await expect(page.locator('details.derived-methodology')).toHaveAttribute('open', '')
    await expect(page.getByText(/Não há fallback para expectativa anual/)).toBeVisible()
    await expectNoOverflow(page)

    if (viewport.width <= 1024) {
      const menu = page.locator('button.menu-toggle')
      await expect(menu).toHaveAccessibleName('Abrir menu')
      await menu.focus()
      await page.keyboard.press('Enter')
      await expect(menu).toHaveAttribute('aria-expanded', 'true')
      await page.locator('#primary-navigation-drawer').getByRole('button', { name: 'Fechar menu' }).focus()
      await page.keyboard.press('Enter')
      await expect(menu).toHaveAttribute('aria-expanded', 'false')
    }

    if (testInfo.project.name === 'chromium') {
      await page.evaluate(() => {
        if (document.activeElement instanceof HTMLElement) document.activeElement.blur()
        window.scrollTo(0, 0)
      })
      await mkdir(evidenceDir, { recursive: true })
      await page.screenshot({
        path: path.join(evidenceDir, `task-13-juro-real-${viewport.width}.png`),
        fullPage: true,
      })
    }
    expect(consoleErrors).toEqual([])
    expect(requestFailures).toEqual([])
  })
}

test('Juro real unavailable empty cache', async ({ page }, testInfo) => {
  await page.setViewportSize({ width: 375, height: 812 })
  await mockShell(page)
  await mockRealRate(page, route => route.fulfill({
    status: 200,
    contentType: 'application/json',
    body: JSON.stringify({ status: 'unavailable', data: {} }),
  }))
  await page.goto('/derivados')
  await expect(page.getByText('Indicador indisponível.')).toBeVisible()
  await expect(page.getByText('O primeiro snapshot elegível ainda não foi calculado.')).toBeVisible()
  await expect(page.getByText('Taxa nominal DI')).toBeVisible()
  await expect(page.locator('details.derived-methodology')).not.toHaveAttribute('open')
  await expect(page.getByText(neutralDisclosure, { exact: true })).toBeVisible()
  await expectNoOverflow(page)
  if (testInfo.project.name === 'chromium') {
    await mkdir(evidenceDir, { recursive: true })
    await page.screenshot({ path: path.join(evidenceDir, 'task-13-juro-real-unavailable.png'), fullPage: true })
  }
  await page.getByText('Como o indicador é calculado').focus()
  await page.keyboard.press('Enter')
  await expect(page.locator('details.derived-methodology')).toHaveAttribute('open', '')
  await expect(page.getByText(/Não há fallback para expectativa anual/)).toBeVisible()
  await page.keyboard.press('Enter')
  await expect(page.locator('details.derived-methodology')).not.toHaveAttribute('open')
})

test('Juro real unavailable HTTP 503', async ({ page }, testInfo) => {
  await page.setViewportSize({ width: 1280, height: 900 })
  await mockShell(page)
  await mockRealRate(page, route => route.fulfill({
    status: 503,
    contentType: 'application/json',
    body: JSON.stringify({ error: 'dataset_unavailable', dataset: 'juro_real_ex_ante_1y' }),
  }))
  await page.goto('/derivados')
  await expect(page.getByRole('alert')).toContainText('HTTP 503')
  await expect(page.getByText(neutralDisclosure, { exact: true })).toBeVisible()
  await expect(page.locator('details.derived-methodology')).toHaveCount(0)
  await expect(page.getByRole('article')).toHaveCount(0)
  await expectNoOverflow(page)
  if (testInfo.project.name === 'chromium') {
    await mkdir(evidenceDir, { recursive: true })
    await page.screenshot({ path: path.join(evidenceDir, 'task-13-juro-real-503.png'), fullPage: true })
  }
})
