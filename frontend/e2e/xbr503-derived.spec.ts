import { expect, test } from '@playwright/test'
import {
  aggregateFixture,
  captureChromiumElement,
  captureChromiumScreenshot,
  expectNoOverflow,
  mockDerived,
  mockDerivedHistory,
  mockShell,
  realRateFixture,
} from './xbr503-fixtures'

for (const viewport of [
  { width: 375, height: 812 },
  { width: 768, height: 1024 },
  { width: 1280, height: 900 },
]) {
  test(`Derivados shows all six independent indicators at ${viewport.width}px`, async ({ page }, testInfo) => {
    // Given
    const consoleErrors: string[] = []
    const requestFailures: string[] = []
    page.on('console', message => {
      if (message.type() === 'error') consoleErrors.push(message.text())
    })
    page.on('requestfailed', request => requestFailures.push(request.url()))
    await page.setViewportSize(viewport)
    await mockShell(page)
    await mockDerived(page, aggregateFixture)

    // When
    await page.goto('/derivados')

    // Then
    const section = page.getByRole('region', { name: 'Outros indicadores derivados' })
    await expect(section.getByRole('article')).toHaveCount(5)
    await expect(section.getByText('140,00%')).toBeVisible()
    await expect(section.getByText('0,35 p.p.')).toBeVisible()
    await expect(section.getByText('10,12 p.p.')).toBeVisible()
    await expect(section.getByText('1,66 p.p.')).toBeVisible()
    await expect(section.getByText('-0,40 p.p.')).toBeVisible()
    await expect(page.getByText('6,84% a.a.').first()).toBeVisible()
    await expect(page.getByRole('note')).toContainText('a página oficial da B3 veda coleta automatizada')
    await expect(section.getByText(/não efeito causal/)).toBeVisible()
    const history = page.getByRole('region', { name: 'Histórico dos indicadores derivados' })
    await expect(history.getByTestId('derived-history-plot')).toBeVisible()
    await expect(history.getByText('Sem snapshots', { exact: true }).first()).toBeVisible()
    await history.getByRole('button', { name: 'Taxas anuais (% a.a.)' }).click()
    await expect(history.getByRole('checkbox', { name: /Juro real ex-ante de um ano/ })).toBeDisabled()
    await expect(history.getByText('Nenhuma série selecionada tem snapshots nesta janela.')).toBeVisible()
    await history.scrollIntoViewIfNeeded()
    await captureChromiumElement(history, testInfo, `derivados-${viewport.width}-history-annual-empty.png`)
    await history.getByRole('button', { name: 'Pontos percentuais (pp)' }).click()
    await expect(history.getByTestId('derived-history-plot')).toBeVisible()
    const spreadSeries = history.getByRole('checkbox', { name: /Spread de crédito PF sobre Selic/ })
    await expect(spreadSeries).toBeChecked()
    await spreadSeries.uncheck()
    await expect(spreadSeries).not.toBeChecked()
    await spreadSeries.check()
    const plottedLines = await history.locator('.recharts-line-curve').evaluateAll(paths =>
      paths.filter(path => (path.getAttribute('d') ?? '').length > 0).length,
    )
    expect(plottedLines).toBeGreaterThan(0)
    await expect(history.locator('.recharts-dot').first()).toBeVisible()
    const firstObservation = history.locator('.derived-history-dot').first()
    await firstObservation.hover()
    await expect(firstObservation.locator('title')).toContainText(
      'Referência: 2026-06-30 · Corte: 30/06/2026, 14:00:12 UTC (as_of: 2026-06-30T14:00:12.345678+00:00)',
    )
    await expectNoOverflow(page)

    await page.evaluate(() => {
      if (document.activeElement instanceof HTMLElement) document.activeElement.blur()
      window.scrollTo(0, 0)
    })
    await captureChromiumScreenshot(page, testInfo, `derivados-${viewport.width}-top.png`)

    const focusCard = section.getByRole('article', { name: 'Distância Focus à meta de inflação' })
    const summary = focusCard.getByText('Metodologia e insumos')
    await summary.focus()
    await page.keyboard.press('Enter')
    await expect(summary.locator('xpath=ancestor::details')).toHaveAttribute('open', '')
    await expect(focusCard.getByText("Filtro Focus: Indicador eq 'IPCA' and Suavizada eq 'S' and baseCalculo eq 0")).toBeVisible()
    await expect(focusCard.getByText(/Regra aplicada:\s*bcb-continuous-target-2025/)).toBeVisible()
    await expect(focusCard.getByText('Versão da tabela: 1.0.0')).toBeVisible()
    await expect(focusCard.getByText('Banda de tolerância: ±1,50 p.p.')).toBeVisible()
    if (viewport.width === 768) {
      const targetBounds = await focusCard.boundingBox()
      const spreadBounds = await section.getByRole('article', { name: 'Spread crédito PF × Selic' }).boundingBox()
      expect(targetBounds).not.toBeNull()
      expect(spreadBounds).not.toBeNull()
      if (targetBounds !== null && spreadBounds !== null) {
        expect(targetBounds.height).toBeGreaterThan(spreadBounds.height)
      }
    }
    await expectNoOverflow(page)

    await captureChromiumElement(section, testInfo, `derivados-${viewport.width}-all-cards.png`)
    await captureChromiumElement(focusCard, testInfo, `derivados-${viewport.width}-focus-card.png`)
    await history.scrollIntoViewIfNeeded()
    await captureChromiumElement(history, testInfo, `derivados-${viewport.width}-history.png`)
    const historyValues = history.getByText('Valores acessíveis e datas de referência')
    await historyValues.focus()
    await page.keyboard.press('Enter')
    await expect(historyValues.locator('xpath=ancestor::details')).toHaveAttribute('open', '')
    if (viewport.width <= 900) {
      await expect(history.locator('.derived-history-list')).toBeVisible()
      await expect(history.locator('.derived-history-table-wrap table')).toBeHidden()
      await expect(history.locator('.derived-history-list').getByText(/Referência:/).first()).toBeVisible()
      await expect(history.locator('.derived-history-list')).toContainText('(as_of: 2026-06-30T14:00:12.345678+00:00)')
    } else {
      await expect(history.locator('.derived-history-table-wrap table')).toBeVisible()
      await expect(history.locator('.derived-history-table-wrap').getByText(/Referência:/).first()).toBeVisible()
      const balanceRow = history.locator('tbody tr').filter({ hasText: '2026-08' })
      const diRow = history.locator('tbody tr').filter({ hasText: '2026-09' })
      const spreadRow = history.locator('tbody tr').filter({ hasText: '2026-06' })
      await expect(balanceRow.locator('td').nth(0)).toContainText('primary_balance_previous_month_missing')
      await expect(diRow.locator('td').nth(2)).toContainText('Corte: 30/09/2026, 14:00:00 UTC (as_of: 2026-09-30T14:00:00Z)')
      await expect(spreadRow.locator('td').nth(1)).toContainText('(as_of: 2026-06-30T14:00:12.345678+00:00)')
      const cellWidths = await history.locator('.derived-history-table-wrap td').evaluateAll(cells =>
        cells.map(cell => ({ content: cell.scrollWidth, available: cell.clientWidth })),
      )
      expect(cellWidths.every(width => width.content <= width.available + 1)).toBe(true)
    }
    await expectNoOverflow(page)
    await captureChromiumElement(history, testInfo, `derivados-${viewport.width}-history-values.png`)
    await captureChromiumScreenshot(page, testInfo, `derivados-${viewport.width}-provenance.png`)
    expect(consoleErrors).toEqual([])
    expect(requestFailures).toEqual([])
  })
}

test('Derivados opens all five additional methodology disclosures with the keyboard', async ({ page }, testInfo) => {
  await page.setViewportSize({ width: 375, height: 812 })
  await page.emulateMedia({ colorScheme: 'dark', reducedMotion: 'reduce' })
  await mockShell(page)
  await mockDerived(page, aggregateFixture)
  await page.goto('/derivados')
  await page.evaluate(() => { document.body.style.zoom = '2' })

  const section = page.getByRole('region', { name: 'Outros indicadores derivados' })
  await expect(section.getByRole('article')).toHaveCount(5)
  const cards = await section.getByRole('article').all()
  for (const card of cards) {
    const summary = card.getByText('Metodologia e insumos')
    await summary.focus()
    await page.keyboard.press('Enter')
    await expect(summary.locator('xpath=ancestor::details')).toHaveAttribute('open', '')
    if (await card.getByRole('heading').textContent() === 'Variação do saldo primário em 12 meses') {
      await expect(card.getByText('0,15% do PIB')).toBeVisible()
      await expect(card.getByText('-0,20% do PIB')).toBeVisible()
      await expect(card.getByText(/Disponível desde:\s*Primeira observação local/)).toHaveCount(2)
      await expectNoOverflow(page)
      if (testInfo.project.name === 'chromium') {
        await captureChromiumElement(card, testInfo, 'xbr503-primary-balance-provenance-375-zoom-2.png')
      }
    }
    await page.keyboard.press('Enter')
    await expect(summary.locator('xpath=ancestor::details')).not.toHaveAttribute('open', '')
  }
  await expectNoOverflow(page)
})

test('Derivados preserves valid siblings beside unavailable snapshots', async ({ page }) => {
  // Given
  await page.setViewportSize({ width: 768, height: 1024 })
  await mockShell(page)
  const { distancia_focus_meta: _unavailable, ...remaining } = aggregateFixture.data
  await mockDerived(page, {
    status: 'ok',
    data: {
      ...remaining,
      distancia_focus_meta: {
        ...aggregateFixture.data.distancia_focus_meta,
        value: null,
        reason: 'inflation_target_missing',
      },
    },
  })

  // When
  await page.goto('/derivados')

  // Then
  const section = page.getByRole('region', { name: 'Outros indicadores derivados' })
  const focusCard = section.getByRole('article', { name: 'Distância Focus à meta de inflação' })
  await expect(focusCard.getByText('Indisponível', { exact: true })).toHaveCount(2)
  await expect(focusCard.getByText('Não há meta de inflação versionada para a data-base.')).toBeVisible()
  await expect(section.getByText('140,00%')).toBeVisible()
  await expect(section.getByText('10,12 p.p.')).toBeVisible()
  await expectNoOverflow(page)
  const sectionTop = await section.evaluate(element => element.getBoundingClientRect().top + window.scrollY)
  await page.evaluate(top => window.scrollTo({ top: top - 60, behavior: 'auto' }), sectionTop)
  await captureChromiumScreenshot(page, test.info(), 'derivados-768-partial.png')
})

test('Derivados empty aggregate cache does not fabricate additional indicator cards', async ({ page }) => {
  // Given
  await page.setViewportSize({ width: 375, height: 812 })
  await mockShell(page)
  await mockDerived(page, { status: 'unavailable', data: {} })

  // When
  await page.goto('/derivados')

  // Then
  const section = page.getByRole('region', { name: 'Outros indicadores derivados' })
  await expect(section.getByText('Nenhum snapshot dos cinco indicadores está disponível no cache.')).toBeVisible()
  await expect(section.getByRole('article')).toHaveCount(0)
  await expect(page.getByText('6,84% a.a.').first()).toBeVisible()
  await expectNoOverflow(page)
  await section.scrollIntoViewIfNeeded()
  await captureChromiumScreenshot(page, test.info(), 'derivados-375-empty.png')
})

test('Derivados reports aggregate HTTP 503 without hiding the real-rate section', async ({ page }) => {
  // Given
  await page.setViewportSize({ width: 1280, height: 900 })
  await mockShell(page)
  await mockDerivedHistory(page)
  await page.route('**/api/derivados', route => route.fulfill({ status: 503, body: 'unavailable' }))
  await page.route('**/api/derivados/juro-real-ex-ante-1y', route => route.fulfill({
    status: 200,
    contentType: 'application/json',
    body: JSON.stringify(realRateFixture),
  }))

  // When
  await page.goto('/derivados')

  // Then
  const section = page.getByRole('region', { name: 'Outros indicadores derivados' })
  await expect(section.getByRole('alert')).toContainText('HTTP 503')
  await expect(section.getByRole('article')).toHaveCount(0)
  await expect(page.getByText('6,84% a.a.').first()).toBeVisible()
  await expectNoOverflow(page)
  await section.scrollIntoViewIfNeeded()
  await captureChromiumScreenshot(page, test.info(), 'derivados-1280-503.png')
})
