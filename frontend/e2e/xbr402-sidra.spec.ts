import { mkdir } from 'node:fs/promises'
import path from 'node:path'

import { expect, test, type Page, type Route } from '@playwright/test'

const evidenceDir = process.env.XBR402_SCREENSHOT_DIR
  ?? '/Users/thiagojesus/.omo/evidence/xbray-roadmap-harness'

const point = (value: number, periodLabel: string) => [{
  reference_period: '202607',
  period_label: periodLabel,
  value,
  available_at: '2026-09-27T12:00:00Z',
  payload_hash: `fixture-${value}`,
}]

const metadata = {
  pim_industria: { unit: 'index', unit_label: 'Número-índice', base_version: '2022=100', frequency: 'monthly', period_label: 'julho 2026' },
  pmc_varejo_ampliado: { unit: 'index', unit_label: 'Número-índice', base_version: '2022=100', frequency: 'monthly', period_label: 'julho 2026' },
  pms_servicos: { unit: 'index', unit_label: 'Número-índice', base_version: '2022=100', frequency: 'monthly', period_label: 'julho 2026' },
  massa_rendimento_real: { unit: 'brl_million', unit_label: 'Milhões de Reais', base_version: 'real_deflator_latest_published_moving_quarter', frequency: 'moving_quarter', period_label: 'mai-jun-jul 2026' },
  taxa_subutilizacao: { unit: 'pct', unit_label: 'Percentual', base_version: null, frequency: 'moving_quarter', period_label: 'mai-jun-jul 2026' },
} as const

const fullSidraPayload = {
  source: 'IBGE SIDRA',
  status: 'ok',
  last_updated: '2026-09-27T12:00:00Z',
  unavailable: [],
  data: {
    pim_industria: point(104.00233, 'julho 2026'),
    pmc_varejo_ampliado: point(106.86857, 'julho 2026'),
    pms_servicos: point(110.5941, 'julho 2026'),
    massa_rendimento_real: point(383472, 'mai-jun-jul 2026'),
    taxa_subutilizacao: point(13, 'mai-jun-jul 2026'),
  },
  metadata,
} as const

const sectorData = {
  ibc_br_sa: [{ data: '01/06/2026', valor: '100' }, { data: '01/07/2026', valor: '101' }],
  ibc_ex_agro_sa: [{ data: '01/06/2026', valor: '99' }, { data: '01/07/2026', valor: '100' }],
  ibc_agro_sa: [{ data: '01/06/2026', valor: '98' }, { data: '01/07/2026', valor: '99' }],
  ibc_industria_sa: [{ data: '01/06/2026', valor: '100' }, { data: '01/07/2026', valor: '101' }],
  ibc_servicos_sa: [{ data: '01/06/2026', valor: '101' }, { data: '01/07/2026', valor: '102' }],
  rendimento_medio_real: [{ data: '01/06/2026', valor: '3000' }, { data: '01/07/2026', valor: '3030' }],
} as const

type SidraMode = 'ok' | 'partial' | 'error'

function fulfillJson(route: Route, body: object, status = 200): Promise<void> {
  return route.fulfill({ status, contentType: 'application/json', body: JSON.stringify(body) })
}

async function mockActivity(page: Page, getSidraMode: () => SidraMode): Promise<void> {
  await page.route('**/*', route => {
    const hostname = new URL(route.request().url()).hostname
    return hostname === '127.0.0.1' ? route.continue() : route.abort('blockedbyclient')
  })
  await page.route('**/api/status', route => fulfillJson(route, { status: 'running', db_stats: {} }))
  await page.route('**/api/atividade/setorial?mode=*', route => {
    const mode = new URL(route.request().url()).searchParams.get('mode')
    return fulfillJson(route, {
      source: 'BCB SGS',
      mode,
      status: 'ok',
      unavailable: [],
      metadata: {},
      data: sectorData,
    })
  })
  await page.route('**/api/atividade', route => fulfillJson(route, {
    data: {
      ibc_br: sectorData.ibc_br_sa,
      desemprego: [{ data: '01/07/2026', valor: '6.5' }],
    },
  }))
  await page.route('**/api/atividade/sidra', route => {
    const mode = getSidraMode()
    if (mode === 'error') {
      return fulfillJson(route, { error: 'dataset_unavailable', dataset: 'atividade_sidra' }, 503)
    }
    if (mode === 'partial') {
      return fulfillJson(route, {
        ...fullSidraPayload,
        status: 'partial',
        unavailable: ['pmc_varejo_ampliado', 'pms_servicos', 'massa_rendimento_real', 'taxa_subutilizacao'],
        data: { pim_industria: fullSidraPayload.data.pim_industria },
        metadata: { pim_industria: metadata.pim_industria },
      })
    }
    return fulfillJson(route, fullSidraPayload)
  })
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
  test(`SIDRA happy periods, partial fallback, and isolation ${viewport.width}`, async ({ page }, testInfo) => {
    let sidraMode: SidraMode = 'ok'
    const consoleErrors: string[] = []
    page.on('console', message => {
      if (message.type() === 'error') consoleErrors.push(message.text())
    })
    await page.setViewportSize(viewport)
    await mockActivity(page, () => sidraMode)
    await page.goto('/atividade')

    await expect(page.getByRole('heading', { name: 'Aprofundamento IBGE/SIDRA' })).toBeVisible()
    await expect(page.locator('.sidra-kpi')).toHaveCount(5)
    await expect(page.getByText('R$ 383.472,0 mi')).toBeVisible()
    await expect(page.getByText('13,0%')).toBeVisible()
    await expect(page.getByText(/Trimestre móvel encerrado em mai-jun-jul 2026/).first()).toBeVisible()
    await expect(page.locator('.sidra-chart')).toHaveCount(3)
    await expectNoOverflow(page)

    if (testInfo.project.name === 'chromium') {
      await mkdir(evidenceDir, { recursive: true })
      await page.screenshot({
        path: path.join(evidenceDir, `task-11-sidra-${viewport.width}-ok.png`),
        fullPage: true,
      })
    }

    sidraMode = 'partial'
    await page.reload()
    await expect(page.getByRole('status')).toContainText('Fonte parcialmente indisponível.')
    await expect(page.getByText('Varejo ampliado')).toBeVisible()
    await expectNoOverflow(page)

    sidraMode = 'error'
    await page.reload()
    await expect(page.getByText('IBC-Br total × ex-agro')).toBeVisible()
    await expect(page.getByRole('alert')).toContainText('Dados SIDRA indisponíveis; séries SGS mantidas.')
    await expect(page.locator('.chart-container')).toHaveCount(3)
    await expectNoOverflow(page)
    await expect(page.locator('body')).not.toContainText(/NaN|Infinity/)
    expect(consoleErrors.filter(message => !message.includes('status of 503'))).toEqual([])
  })
}
