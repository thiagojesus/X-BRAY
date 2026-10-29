import { readFileSync } from 'node:fs'
import { mkdir } from 'node:fs/promises'
import path from 'node:path'

import { expect, test, type Page, type Route } from '@playwright/test'
import { syntheticModalities } from '../src/test/creditoModalities'

type SeriesPoint = {
  readonly data: string
  readonly valor: string
}

type CreditContract = {
  readonly data: Readonly<Record<string, readonly SeriesPoint[]>>
  readonly status: 'ok' | 'partial'
  readonly unavailable: readonly string[]
  readonly metadata: Readonly<Record<string, Readonly<Record<string, string | number>>>>
}

const evidenceDir = process.env.XBR202_SCREENSHOT_DIR
  ?? path.join(process.cwd(), 'test-results', 'evidence', 'xbr202')
const generatedContractPath = process.env.XBRAY_CREDIT_CONTRACT_FIXTURE
const fixturePath = generatedContractPath
  ? path.resolve(generatedContractPath)
  : path.resolve(process.cwd(), '../backend/tests/fixtures/contracts/credito.json')
const contract = JSON.parse(readFileSync(fixturePath, 'utf8')) as CreditContract

const creditPayload = generatedContractPath ? contract : {
  ...contract,
  last_updated: '2026-09-21T12:00:00Z',
  data: {
    ...contract.data,
    inadimplencia_pf: [
      { data: '01/07/2026', valor: '3.2' },
      ...contract.data.inadimplencia_pf,
    ],
    endividamento_familias: [
      { data: '01/06/2026', valor: '48.5' },
      ...contract.data.endividamento_familias,
    ],
    comprometimento_renda: [
      { data: '01/07/2026', valor: '27.5' },
      ...contract.data.comprometimento_renda,
    ],
    icc_pf: [
      { data: '01/07/2026', valor: '22.7' },
      ...contract.data.icc_pf,
    ],
    taxa_media_credito_pf: [
      { data: '01/07/2026', valor: '14.4' },
      ...contract.data.taxa_media_credito_pf,
    ],
    carteira_pf_cheque_especial: [{ data: '01/01/2026', valor: '9999' }],
    carteira_pf_credito_pessoal_nao_consignado: [{ data: '01/01/2026', valor: '9999' }],
    carteira_pf_cartao_rotativo: [{ data: '01/01/2026', valor: '9999' }],
    carteira_pf_cartao_parcelado: [{ data: '01/01/2026', valor: '9999' }],
  },
  modalities: syntheticModalities,
  metadata: {
    ...contract.metadata,
    carteira_pf_cheque_especial: { code: 20573, unit: 'brl_million', frequency: 'monthly', reference_period: '2026-01-01', deadline: '2026-03-17', freshness: 'ok' },
    carteira_pf_credito_pessoal_nao_consignado: { code: 20574, unit: 'brl_million', frequency: 'monthly', reference_period: '2026-01-01', deadline: '2026-03-17', freshness: 'ok' },
    carteira_pf_cartao_rotativo: { code: 20587, unit: 'brl_million', frequency: 'monthly', reference_period: '2026-01-01', deadline: '2026-03-17', freshness: 'ok' },
    carteira_pf_cartao_parcelado: { code: 20588, unit: 'brl_million', frequency: 'monthly', reference_period: '2026-01-01', deadline: '2026-03-17', freshness: 'ok' },
  },
}

const interestPayload = {
  data: {
    selic_meta: [
      { data: '01/07/2026', valor: '13.75' },
      { data: '01/08/2026', valor: '14.00' },
      { data: '29/08/2026', valor: '14.25' },
      { data: '15/09/2026', valor: '14.50' },
    ],
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
    body: JSON.stringify({ status: 'running', db_stats: {}, last_updated: '2026-09-21T12:00:00Z' }),
  }))
  await page.route('**/api/juros', route => route.fulfill({
    status: 200,
    contentType: 'application/json',
    body: JSON.stringify(interestPayload),
  }))
}

async function mockCredit(page: Page, handler: (route: Route) => Promise<void> | void): Promise<void> {
  await page.route('**/api/credito', handler)
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

async function expectAxisLabelsContained(page: Page): Promise<void> {
  const charts = page.locator('.chart-container')
  for (let index = 0; index < await charts.count(); index += 1) {
    const geometry = await charts.nth(index).evaluate(element => {
      const svgRect = element.querySelector('.recharts-surface')?.getBoundingClientRect()
      if (svgRect === undefined) return null
      return {
        svg: {
          left: svgRect.left,
          top: svgRect.top,
          right: svgRect.right,
          bottom: svgRect.bottom,
        },
        labels: [...element.querySelectorAll('.recharts-label')].map(label => {
          const rect = label.getBoundingClientRect()
          return {
            text: label.textContent ?? '',
            left: rect.left,
            top: rect.top,
            right: rect.right,
            bottom: rect.bottom,
          }
        }),
      }
    })
    expect(geometry).not.toBeNull()
    if (geometry === null) continue
    for (const label of geometry.labels) {
      expect(label.left, `${label.text} crosses the SVG left edge`).toBeGreaterThanOrEqual(geometry.svg.left - 1)
      expect(label.top, `${label.text} crosses the SVG top edge`).toBeGreaterThanOrEqual(geometry.svg.top - 1)
      expect(label.right, `${label.text} crosses the SVG right edge`).toBeLessThanOrEqual(geometry.svg.right + 1)
      expect(label.bottom, `${label.text} crosses the SVG bottom edge`).toBeLessThanOrEqual(geometry.svg.bottom + 1)
    }
  }
}

async function expectReferenceDatesInTooltip(page: Page): Promise<void> {
  const chart = page.locator('.chart-container').filter({ hasText: 'Selic meta × taxa média do crédito PF' })
  const grid = chart.locator('.recharts-cartesian-grid')
  await grid.scrollIntoViewIfNeeded()
  const gridBox = await grid.boundingBox()
  expect(gridBox).not.toBeNull()
  if (gridBox === null) return

  await page.mouse.move(
    gridBox.x + gridBox.width - 2,
    gridBox.y + gridBox.height / 2,
  )
  const tooltip = chart.locator('.recharts-tooltip-wrapper')
  await expect(tooltip).toContainText('ref. 15/09/2026')
  await expect(tooltip).toContainText('ref. 01/09/2026')
}

for (const viewport of [
  { width: 375, height: 812 },
  { width: 768, height: 1024 },
  { width: 1280, height: 900 },
]) {
  test(`Credito happy responsive ${viewport.width}`, async ({ page }, testInfo) => {
    const consoleErrors: string[] = []
    const requestFailures: string[] = []
    page.on('console', message => {
      if (message.type() === 'error') consoleErrors.push(message.text())
    })
    page.on('requestfailed', request => requestFailures.push(`${request.url()}: ${request.failure()?.errorText ?? 'unknown'}`))
    await page.setViewportSize(viewport)
    await mockShell(page)
    await mockCredit(page, route => route.fulfill({
      status: 200,
      contentType: 'application/json',
      body: JSON.stringify(creditPayload),
    }))

    await page.goto('/')
    await page.getByRole('link', { name: /Crédito e Famílias/ }).last().click()

    await expect(page).toHaveURL(/\/credito$/)
    await expect(page.locator('header.main-header')).toContainText('Crédito e Famílias')
    await expect(page.locator('.sidebar-nav a[href="/credito"]')).toHaveAttribute('aria-current', 'page')
    for (const label of [
      'Inadimplência PF',
      'Endividamento das famílias',
      'Comprometimento de renda',
      'ICC PF',
      'Taxa média do crédito PF',
    ]) {
      await expect(kpiCard(page, label).locator('.kpi-label')).toContainText(label)
    }
    await expect(kpiCard(page, 'Inadimplência PF').locator('.kpi-value')).toHaveText('3,4%')
    await expect(kpiCard(page, 'Taxa média do crédito PF').locator('.kpi-value')).toHaveText('14,20% a.a.')
    await expect(page.getByText('Comprometimento de renda × inadimplência PF')).toBeVisible()
    await expect(page.getByText('Selic meta × taxa média do crédito PF')).toBeVisible()
    await expect(page.getByText('Endividamento × comprometimento de renda')).toBeVisible()
    await expect(page.getByRole('heading', { name: 'Qualidade e estresse do crédito PF' })).toBeVisible()
    await expect(page.getByRole('table', { name: 'Saldos nominais e variações mensais e anuais por modalidade' })).toBeVisible()
    await expect(page.getByRole('row', { name: /Cheque especial SGS 20573/ })).toContainText('R$ 120,5 mi')
    await expect(page.getByRole('row', { name: /Crédito pessoal não consignado SGS 20574/ })).toContainText('Crédito pessoal não consignado total')
    await expect(page.getByRole('row', { name: /Cartão parcelado com juros SGS 20588/ })).toBeVisible()
    await expect(page.getByRole('link', { name: 'Abrir dados oficiais do SGS, série 20574' })).toHaveAttribute(
      'href', 'https://api.bcb.gov.br/dados/serie/bcdata.sgs.20574/dados',
    )
    await expect(page.locator('.chart-container')).toHaveCount(5)
    const stockChart = page.locator('.chart-container').filter({ hasText: 'Saldos nominais das modalidades selecionadas' })
    const yoyChart = page.locator('.chart-container').filter({ hasText: 'Variação dos saldos em 12 meses por modalidade' })
    await expect(stockChart.locator('.recharts-line')).toHaveCount(4)
    await expect(yoyChart.locator('.recharts-line')).toHaveCount(4)
    await expect(yoyChart).toContainText('%')
    const modalityTableRegion = page.getByRole('region', { name: 'Tabela das modalidades de crédito PF' })
    if (viewport.width >= 768) {
      const tableGeometry = await modalityTableRegion.evaluate(element => {
        const table = element.querySelector('table')
        const lastHeader = table?.querySelector('thead th:last-child')
        const container = element.getBoundingClientRect()
        const lastColumn = lastHeader?.getBoundingClientRect()
        return {
          clientWidth: element.clientWidth,
          scrollWidth: element.scrollWidth,
          containerRight: container.right,
          lastColumnRight: lastColumn?.right ?? 0,
        }
      })
      expect(tableGeometry.scrollWidth).toBeLessThanOrEqual(tableGeometry.clientWidth + 1)
      expect(tableGeometry.lastColumnRight).toBeLessThanOrEqual(tableGeometry.containerRight + 1)
    } else {
      await modalityTableRegion.focus()
      await page.keyboard.press('ArrowRight')
      await page.keyboard.press('ArrowRight')
      await expect.poll(() => modalityTableRegion.evaluate(element => element.scrollLeft)).toBeGreaterThan(0)
    }
    await expect(page.getByText(/mecanismo econômico geral, não como prova causal/i)).toBeVisible()
    const summary = page.locator('.info-box').nth(1)
    await expect(summary.locator('p').nth(1)).toContainText('Custo do crédito')
    await expect(summary.locator('p').nth(2)).toContainText('Comprometimento de renda')
    await expect(summary.locator('p').nth(3)).toContainText('Inadimplência PF')
    await expectNoOverflow(page)
    await expectAxisLabelsContained(page)

    if (viewport.width === 1280) await expectReferenceDatesInTooltip(page)

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
      await page.evaluate(() => window.scrollTo(0, 0))
      await expect.poll(() => page.evaluate(() => window.scrollY)).toBe(0)
      await page.waitForTimeout(800)
      await mkdir(evidenceDir, { recursive: true })
      await page.screenshot({
        path: path.join(evidenceDir, `task-7-credito-${viewport.width}.png`),
        fullPage: true,
      })
    }
    expect(consoleErrors).toEqual([])
    expect(requestFailures).toEqual([])
  })
}

test('Credito partial stale failure preserves cards and explicit gaps', async ({ page }) => {
  const partialPayload = {
    ...creditPayload,
    status: 'partial',
    unavailable: ['taxa_media_credito_pf'],
    data: { ...creditPayload.data, taxa_media_credito_pf: [] },
    metadata: {
      ...creditPayload.metadata,
      comprometimento_renda: { ...creditPayload.metadata.comprometimento_renda, freshness: 'stale' },
    },
  }
  await mockShell(page)
  await mockCredit(page, route => route.fulfill({
    status: 200,
    contentType: 'application/json',
    body: JSON.stringify(partialPayload),
  }))

  await page.goto('/credito')

  await expect(page.getByRole('status').filter({ hasText: 'Fonte parcialmente indisponível.' })).toContainText('Fonte parcialmente indisponível.')
  await expect(kpiCard(page, 'Inadimplência PF').locator('.kpi-value')).toHaveText('3,4%')
  await expect(kpiCard(page, 'Taxa média do crédito PF').locator('.kpi-value')).toHaveText('Indisponível')
  await expect(page.getByText(/Custo do crédito: dados insuficientes/i)).toBeVisible()
  await expect(page.locator('body')).not.toContainText('NaN')
  await expectNoOverflow(page)
})

test('Credito remains compatible when an older backend response has no modality block', async ({ page }) => {
  const { modalities: _modalities, ...legacyPayload } = creditPayload
  await mockShell(page)
  await mockCredit(page, route => route.fulfill({
    status: 200,
    contentType: 'application/json',
    body: JSON.stringify(legacyPayload),
  }))
  await page.setViewportSize({ width: 375, height: 812 })
  await page.goto('/credito')
  await expect(kpiCard(page, 'Inadimplência PF').locator('.kpi-value')).toHaveText('3,4%')
  await expect(page.locator('.credit-exposure-unavailable')).toContainText('Dados das modalidades de crédito não vieram')
  await expect(page.locator('.chart-container')).toHaveCount(3)
  await expectNoOverflow(page)
})

test('Credito unavailable failure reports HTTP 503', async ({ page }) => {
  await mockShell(page)
  await mockCredit(page, route => route.fulfill({
    status: 503,
    contentType: 'application/json',
    body: '{"error":"dataset_unavailable","dataset":"credito"}',
  }))

  await page.goto('/credito')

  await expect(page.getByRole('alert')).toContainText('Dados indisponíveis.')
  await expect(page.getByRole('alert')).toContainText('HTTP 503')
  await expectNoOverflow(page)
})

test('Credito Selic failure remains explicit while preserving credit data', async ({ page }) => {
  await mockShell(page)
  await page.route('**/api/juros', route => route.fulfill({
    status: 503,
    contentType: 'application/json',
    body: '{"error":"dataset_unavailable","dataset":"juros"}',
  }))
  await mockCredit(page, route => route.fulfill({
    status: 200,
    contentType: 'application/json',
    body: JSON.stringify(creditPayload),
  }))

  await page.goto('/credito')

  await expect(page.locator('.data-quality-notice').filter({ hasText: 'Selic meta indisponível.' })).toContainText('Selic meta indisponível.')
  await expect(page.locator('.data-quality-notice').filter({ hasText: 'Selic meta indisponível.' })).toContainText('somente os dados de crédito disponíveis')
  await expect(kpiCard(page, 'Taxa média do crédito PF').locator('.kpi-value')).toHaveText('14,20% a.a.')
  await expect(page.getByText('Selic meta × taxa média do crédito PF')).toBeVisible()
  await expectNoOverflow(page)
})
