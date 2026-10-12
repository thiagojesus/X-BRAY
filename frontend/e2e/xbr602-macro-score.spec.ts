import { expect, type Locator, type Page, type TestInfo, test } from '@playwright/test'
import { DIMENSION_LABELS } from '../src/components/macro-score/format'
import { DIMENSION_KEYS } from '../src/components/macro-score/schema'
import { macroScoreFixture, macroScoreHistoryFixture, partialScoreFixture, readySnapshot, unavailableScoreFixture } from '../src/test/macroScoreFixtures'
import { expectNoOverflow, mockShell } from './xbr503-fixtures'

const viewports = [{ width: 375, height: 812 }, { width: 768, height: 1024 }, { width: 1280, height: 900 }] as const

async function mockScore(page: Page, latest: unknown, history: unknown = macroScoreHistoryFixture): Promise<void> {
  await mockShell(page)
  await page.route('**/api/macro-score', route => route.fulfill({ json: latest }))
  await page.route('**/api/macro-score/history?*', route => route.fulfill({ json: history }))
}

async function toggleMethodology(page: Page, screenshotPath?: string): Promise<void> {
  const summary = page.getByText('Metodologia Macro Score Beta', { exact: true })
  await summary.focus()
  await page.keyboard.press('Enter')
  await expect(page.locator('.macro-methodology')).toHaveAttribute('open', '')
  await expect(page.getByRole('heading', { name: 'Metodologia 0.1.0-beta' })).toBeVisible()
  if (screenshotPath !== undefined) {
    await expectNoOverflow(page)
    await page.screenshot({ path: screenshotPath, fullPage: true })
  }
  await page.keyboard.press('Enter')
  await expect(page.locator('.macro-methodology')).not.toHaveAttribute('open')
  await expect(summary).toBeFocused()
}

async function expectReadableText(page: Page): Promise<void> {
  const contrasts = await page.locator('.macro-page').evaluate(root => {
    function luminance(color: string): number {
      const rgb = color.match(/^rgba?\((\d+),\s*(\d+),\s*(\d+)/)
      if (rgb === null) throw new TypeError(`Unsupported computed color: ${color}`)
      const linear = [Number(rgb[1]), Number(rgb[2]), Number(rgb[3])].map(channel => {
        const value = channel / 255
        return value <= 0.04045 ? value / 12.92 : ((value + 0.055) / 1.055) ** 2.4
      })
      return 0.2126 * linear[0] + 0.7152 * linear[1] + 0.0722 * linear[2]
    }
    return Array.from(root.querySelectorAll('.macro-value, .macro-beta, .macro-dimension header span')).map(element => {
      const panel = element.closest('.macro-panel') ?? root
      const foreground = luminance(getComputedStyle(element).color)
      const background = luminance(getComputedStyle(panel).backgroundColor)
      return (Math.max(foreground, background) + 0.05) / (Math.min(foreground, background) + 0.05)
    })
  })
  for (const contrast of contrasts) expect(contrast).toBeGreaterThanOrEqual(4.5)
}

async function expectHistoryValueReadable(value: Locator, maxLines: number, testInfo: TestInfo): Promise<void> {
  await expect(value).toBeVisible()
  const geometry = await value.evaluate(element => {
    const entry = element.closest('li')
    if (entry === null) throw new TypeError('History values must belong to native list items')
    const style = getComputedStyle(entry)
    const zoom = Number(getComputedStyle(document.body).zoom)
    const range = document.createRange()
    range.selectNodeContents(element)
    const bounds = element.getBoundingClientRect()
    const rects = Array.from(range.getClientRects()).filter(rect => rect.width > 0 && rect.height > 0)
    return {
      text: element.textContent,
      valueWidth: bounds.width / zoom,
      fullWidth: entry.clientWidth - Number.parseFloat(style.paddingLeft) - Number.parseFloat(style.paddingRight),
      textOverflow: getComputedStyle(element).textOverflow,
      lines: new Set(rects.map(rect => rect.top)).size,
      excursions: rects.map(rect => Math.max(bounds.left - rect.left, rect.right - bounds.right, bounds.top - rect.top, rect.bottom - bounds.bottom) / zoom),
    }
  })
  await testInfo.attach('history-value-geometry', { body: JSON.stringify(geometry), contentType: 'application/json' })
  expect.soft(Math.abs(geometry.valueWidth - geometry.fullWidth)).toBeLessThanOrEqual(1)
  expect.soft(geometry.textOverflow).not.toBe('ellipsis')
  expect.soft(geometry.lines).toBeGreaterThan(0)
  expect.soft(geometry.lines).toBeLessThanOrEqual(maxLines)
  for (const excursion of geometry.excursions) expect.soft(excursion).toBeLessThanOrEqual(1)
}

for (const populated of [true, false]) {
  for (const zoom of [1, 2]) {
    test(`Macro Score history mobile readability ${populated ? 'populated' : 'unavailable'} 375 zoom ${zoom}`, async ({ page }, testInfo) => {
      // Given
      await page.setViewportSize({ width: 375, height: 812 })
      await page.emulateMedia({ colorScheme: 'dark', reducedMotion: 'reduce' })
      await mockScore(page, populated ? macroScoreFixture : unavailableScoreFixture, populated ? macroScoreHistoryFixture : { status: 'ok', data: [unavailableScoreFixture.data] })
      await page.goto('/macro-score')
      await page.evaluate(value => { document.body.style.zoom = String(value) }, zoom)
      const history = page.getByRole('region', { name: 'Histórico persistido' })
      // When
      await history.getByText('Valores acessíveis: Score geral', { exact: true }).focus()
      await page.keyboard.press('Enter')
      // Then
      await expect(history.locator('details')).toHaveAttribute('open', '')
      await expectNoOverflow(page)
      await history.locator('details').getByText('2026-09', { exact: true }).filter({ visible: true }).scrollIntoViewIfNeeded()
      if (testInfo.project.name === 'chromium') {
        await history.getByRole('listitem').filter({ has: page.getByText('2026-09', { exact: true }) }).screenshot({ path: testInfo.outputPath(`macro-history-mobile-row-${populated ? 'populated' : 'unavailable'}-375-zoom-${zoom}.png`) })
      }
      const list = history.getByRole('list')
      await expect(list).toBeVisible()
      expect(await list.evaluate(element => element.tagName)).toMatch(/^(UL|OL)$/)
      await expect(list.getByRole('listitem')).toHaveCount(120)
      await expect(history.getByRole('table')).toHaveCount(0)
      await expect(history.locator('table')).not.toBeVisible()
      for (const { month, score, state, cutoff } of [
        { month: '2026-09', score: populated ? '73,125' : 'Indisponível', state: populated ? 'Disponível' : 'Score indisponível', cutoff: '30/09/2026, 12:00:00 UTC' },
        ...(populated ? [{ month: '2026-07', score: 'Indisponível', state: 'Score indisponível', cutoff: '30/09/2026, 12:00:00 UTC' }] : []),
        { month: '2026-08', score: 'Indisponível', state: 'Sem snapshot', cutoff: 'Não disponível' },
      ]) {
        const entry = list.getByRole('listitem').filter({ has: page.getByText(month, { exact: true }) })
        await expect(entry.getByRole('term')).toHaveText(['Mês-base', 'Score', 'Estado', 'Corte de informação (as_of)'])
        const values = entry.getByRole('definition')
        await expect(values).toHaveText([month, score, state, cutoff])
        await expectHistoryValueReadable(values.nth(1), 2, testInfo)
        await expectHistoryValueReadable(values.nth(2), 2, testInfo)
        await expectHistoryValueReadable(values.nth(3), 3, testInfo)
      }
      await expectNoOverflow(page)
    })
  }
}

for (const width of [560, 561, 768, 1280]) {
  test(`Macro Score history responsive values ${width}`, async ({ page }) => {
    // Given
    await page.setViewportSize({ width, height: 900 })
    await mockScore(page, macroScoreFixture)
    await page.goto('/macro-score')
    const history = page.getByRole('region', { name: 'Histórico persistido' })
    // When
    await history.getByText('Valores acessíveis: Score geral', { exact: true }).focus()
    await page.keyboard.press('Enter')
    // Then
    if (width <= 560) {
      await expect(history.getByRole('list')).toBeVisible()
      await expect(history.getByRole('listitem')).toHaveCount(120)
      await expect(history.getByRole('table')).toHaveCount(0)
      await expect(history.locator('table')).not.toBeVisible()
    } else {
      const table = history.getByRole('table')
      await expect(table).toBeVisible()
      await expect(table).toHaveJSProperty('tagName', 'TABLE')
      await expect(table).toHaveAccessibleName('Score geral · pontos de 0 a 100 · snapshots da API')
      await expect(table.getByRole('columnheader')).toHaveText(['Mês-base', 'Score', 'Estado / corte'])
      await expect(table.getByRole('rowheader')).toHaveCount(120)
      await expect(table.getByRole('rowheader').first()).toHaveText('2016-10')
      await expect(table.getByRole('rowheader').last()).toHaveText('2026-09')
      await expect(table.getByRole('row').filter({ has: page.getByRole('rowheader', { name: '2026-09', exact: true }) }).getByRole('cell')).toHaveText(['73,125', '30/09/2026, 12:00:00 UTC'])
      await expect(history.getByRole('list')).toHaveCount(0)
      await expect(history.locator('ul, ol')).not.toBeVisible()
    }
    await expectNoOverflow(page)
  })
}

for (const viewport of viewports) {
  test(`Macro Score Home discovery ${viewport.width}`, async ({ page }, testInfo) => {
    // Given
    await page.setViewportSize(viewport)
    await page.emulateMedia({ colorScheme: 'dark', reducedMotion: 'reduce' })
    await mockShell(page)
    // When
    await page.goto('/')
    // Then
    const card = page.locator('.dashboard-grid').getByRole('link', { name: /Macro Score Beta/ })
    await expect(card).toBeVisible()
    await expect(card).toBeInViewport({ ratio: 1 })
    await expect(card).toHaveAttribute('href', '/macro-score')
    await expect(page.getByText('Caches: 0 registros armazenados')).toBeVisible()
    await expectNoOverflow(page)
    if (testInfo.project.name === 'chromium') {
      await page.screenshot({ path: testInfo.outputPath(`macro-score-home-${viewport.width}-dark.png`), fullPage: true })
    }
  })

  for (const colorScheme of ['light', 'dark'] as const) {
    test(`Macro Score happy drilldown ${viewport.width} ${colorScheme}`, async ({ page }, testInfo) => {
      // Given
      if (testInfo.project.name === 'webkit' && viewport.width === 375) {
        test.setTimeout(90_000)
      }
      const errors: string[] = []
      const summaries: string[] = []
      page.on('pageerror', error => errors.push(error.message))
      page.on('console', message => { if (message.type() === 'error') errors.push(message.text()) })
      page.on('request', request => { if (request.url().includes('/api/macro-score/summary')) summaries.push(request.url()) })
      await page.setViewportSize(viewport)
      await page.emulateMedia({ colorScheme, reducedMotion: 'reduce' })
      await mockScore(page, macroScoreFixture)
      // When
      await page.goto('/')
      await page.getByRole('link', { name: /Macro Score Beta/ }).last().click()
      // Then
      await expect(page).toHaveURL(/\/macro-score$/)
      await expect(page.getByTestId('macro-score-value')).toHaveText('73,125')
      await expect(page.getByText('6/6 · 100%')).toBeVisible()
      await expect(page.getByText('Forte', { exact: true })).toBeVisible()
      await expect(page.locator('.sidebar-nav a[href="/macro-score"]')).toHaveAttribute('aria-current', 'page')
      await expect(page.getByRole('article')).toHaveCount(6)
      await expectNoOverflow(page)
      await page.screenshot({ path: testInfo.outputPath(`macro-score-${viewport.width}-${colorScheme}.png`), fullPage: true })

      const captureDisclosures = testInfo.project.name === 'chromium' && viewport.width === 1280 && colorScheme === 'dark'
      for (const key of DIMENSION_KEYS) {
        const dimension = page.getByRole('article', { name: DIMENSION_LABELS[key], exact: true })
        const summary = dimension.locator(':scope > details > summary')
        await summary.focus()
        await page.keyboard.press('Enter')
        const components = dimension.locator('.macro-component')
        await expect(components).toHaveCount(readySnapshot.dimensions[key].total_components)
        for (const [componentIndex, component] of (await components.all()).entries()) {
          const inputs = component.locator('summary')
          await inputs.focus()
          await page.keyboard.press('Enter')
          await expect(component.locator('.macro-input').first()).toBeVisible()
          await expect(component).toContainText('Disponível em')
          await expect(component).toContainText('Fonte')
          await expect(inputs).toBeFocused()
          await expectNoOverflow(page)
          if (captureDisclosures && key === 'activity' && componentIndex === 0) {
            await page.evaluate(() => window.scrollTo(0, 0))
            await page.screenshot({ path: testInfo.outputPath('macro-score-1280-dark-provenance-open.png'), fullPage: true })
          }
          await page.keyboard.press('Enter')
          await expect(component.locator('details')).not.toHaveAttribute('open')
        }
        await summary.focus()
        await page.keyboard.press('Enter')
        await expect(dimension.locator(':scope > details')).not.toHaveAttribute('open')
      }
      await toggleMethodology(page, captureDisclosures ? testInfo.outputPath('macro-score-1280-dark-methodology-open.png') : undefined)
      const history = page.getByRole('region', { name: 'Histórico persistido' })
      await expect(history).toContainText('2016-10 a 2026-09')
      await history.getByText('Valores acessíveis: Score geral').focus()
      await page.keyboard.press('Enter')
      const historyEntry = (month: string) => viewport.width <= 560
        ? history.getByRole('listitem').filter({ has: page.getByText(month, { exact: true }) })
        : history.getByRole('row').filter({ has: page.getByRole('rowheader', { name: month, exact: true }) })
      await expect(historyEntry('2026-07')).toContainText('Score indisponível')
      await expect(historyEntry('2026-08')).toContainText('Sem snapshot')
      await expect(historyEntry('2026-09')).toContainText('73,125')
      for (const key of DIMENSION_KEYS) {
        await history.getByRole('combobox').selectOption(key)
        await expect(history.getByRole('heading', { name: `Histórico: ${DIMENSION_LABELS[key]}` })).toBeVisible()
        await expect(historyEntry('2026-09')).toContainText(String(readySnapshot.dimensions[key].score).replace('.', ','))
      }
      await page.emulateMedia({ colorScheme, reducedMotion: 'reduce', forcedColors: 'active' })
      await expectReadableText(page)
      await toggleMethodology(page)
      await page.evaluate(() => { document.body.style.zoom = '2' })
      await expectNoOverflow(page)
      await toggleMethodology(page)
      await page.screenshot({ path: testInfo.outputPath(`macro-score-${viewport.width}-zoom-forced.png`) })
      expect(errors).toEqual([])
      expect(summaries).toEqual([])
    })
  }

  test(`Macro Score failure accessibility partial stale empty ${viewport.width}`, async ({ page }, testInfo) => {
    // Given
    const errors: string[] = []
    page.on('pageerror', error => errors.push(error.message))
    await page.setViewportSize(viewport)
    await page.emulateMedia({ colorScheme: 'light', reducedMotion: 'reduce', forcedColors: 'active' })
    await mockScore(page, partialScoreFixture, { status: 'unavailable', data: [] })
    let fail = false
    await page.route('**/api/macro-score', route => fail ? route.fulfill({ status: 503, json: { error: 'dataset_unavailable' } }) : route.fulfill({ json: partialScoreFixture }))
    // When
    await page.goto('/macro-score')
    // Then
    await expect(page.getByTestId('macro-score-value')).toHaveText('69,875')
    await expect(page.getByText('5/6 · 83,33%')).toBeVisible()
    await expect(page.getByText(/Cobertura parcial das dimensões/)).toBeVisible()
    await expect(page.getByText(/Histórico vazio:/)).toBeVisible()
    await expectReadableText(page)
    await toggleMethodology(page)
    fail = true
    await page.getByRole('button', { name: 'Tentar Macro Score novamente' }).focus()
    await page.keyboard.press('Enter')
    await expect(page.getByRole('alert')).toContainText('HTTP 503')
    await expect(page.getByText(/Snapshot em cache possivelmente desatualizado/)).toBeVisible()
    await expect(page.getByTestId('macro-score-value')).toHaveText('69,875')
    await expect(page.getByText('5/6 · 83,33%')).toBeVisible()
    await toggleMethodology(page)
    await expectNoOverflow(page)
    await page.screenshot({ path: testInfo.outputPath(`macro-score-${viewport.width}-stale.png`), fullPage: true })
    expect(errors).toEqual([])
  })
}

test('Macro Score failure accessibility 503 and history error', async ({ page }, testInfo) => {
  // Given
  await page.setViewportSize({ width: 375, height: 812 })
  await page.emulateMedia({ reducedMotion: 'reduce', colorScheme: 'dark' })
  await mockShell(page)
  await page.route('**/api/macro-score', route => route.fulfill({ status: 503, json: { error: 'dataset_unavailable' } }))
  await page.route('**/api/macro-score/history?*', route => route.fulfill({ status: 422, json: { error: 'invalid_macro_score_range' } }))
  // When
  await page.goto('/macro-score')
  // Then
  await expect(page.getByRole('alert').first()).toContainText('HTTP 503')
  await expect(page.getByRole('alert').last()).toContainText('Erro no histórico: Falha de transporte: HTTP 422')
  await expect(page.getByTestId('macro-score-value')).toHaveCount(0)
  await expect(page.getByText(/Histórico vazio:/)).toHaveCount(0)
  await expect(page.getByText(/Sem snapshot persistido\./)).toHaveCount(0)
  await toggleMethodology(page)
  await expectNoOverflow(page)
  await page.screenshot({ path: testInfo.outputPath('macro-score-375-http-errors.png'), fullPage: true })
})

for (const persisted of [true, false]) {
  test(`Macro Score failure accessibility unavailable persisted ${persisted}`, async ({ page }, testInfo) => {
    // Given
    await page.setViewportSize({ width: 768, height: 1024 })
    await mockScore(page, persisted ? unavailableScoreFixture : { status: 'unavailable', data: null }, { status: 'unavailable', data: [] })
    // When
    await page.goto('/macro-score')
    // Then
    await expect(page.getByText(/Histórico vazio:/)).toBeVisible()
    if (persisted) {
      await expect(page.getByTestId('macro-score-value')).toHaveText('Indisponível')
      await expect(page.getByText('4/6 · 66,67%')).toBeVisible()
      await expect(page.getByRole('article')).toHaveCount(6)
    } else {
      await expect(page.getByText(/Sem snapshot persistido\./)).toBeVisible()
      await expect(page.getByTestId('macro-score-value')).toHaveCount(0)
    }
    await toggleMethodology(page)
    await expectNoOverflow(page)
    await page.screenshot({ path: testInfo.outputPath(`macro-score-768-unavailable-${persisted}.png`), fullPage: true })
  })
}
