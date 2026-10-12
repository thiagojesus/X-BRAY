import { mkdir } from 'node:fs/promises'
import path from 'node:path'
import type { Locator, Page, TestInfo } from '@playwright/test'
import { aggregateFixture, realRateFixture } from '../src/test/derivedFixtures'

export const evidenceDir = process.env.XBR503_SCREENSHOT_DIR
  ?? '/Users/thiagojesus/.omo/evidence/xbray-roadmap-harness/task-14-xbr503'

export async function captureChromiumScreenshot(
  page: Page,
  testInfo: TestInfo,
  filename: string,
): Promise<void> {
  if (testInfo.project.name !== 'chromium') return
  await mkdir(evidenceDir, { recursive: true })
  await page.screenshot({ path: path.join(evidenceDir, filename) })
}

export async function captureChromiumElement(
  element: Locator,
  testInfo: TestInfo,
  filename: string,
): Promise<void> {
  if (testInfo.project.name !== 'chromium') return
  await mkdir(evidenceDir, { recursive: true })
  await element.screenshot({
    path: path.join(evidenceDir, filename),
    style: 'header.main-header { visibility: hidden !important; }',
  })
}

export async function mockShell(page: Page): Promise<void> {
  await page.route('**/*', route => {
    const hostname = new URL(route.request().url()).hostname
    return hostname === '127.0.0.1' ? route.continue() : route.abort('blockedbyclient')
  })
  await page.route('**/api/status', route => route.fulfill({
    status: 200,
    contentType: 'application/json',
    body: JSON.stringify({ status: 'running', db_stats: {}, last_updated: '2026-09-27T12:00:00Z' }),
  }))
}

export async function mockDerived(page: Page, aggregate: unknown): Promise<void> {
  await page.route('**/api/derivados', route => route.fulfill({
    status: 200,
    contentType: 'application/json',
    body: JSON.stringify(aggregate),
  }))
  await page.route('**/api/derivados/juro-real-ex-ante-1y', route => route.fulfill({
    status: 200,
    contentType: 'application/json',
    body: JSON.stringify(realRateFixture),
  }))
}

export async function expectNoOverflow(page: Page): Promise<void> {
  const widths = await page.evaluate(() => ({
    viewport: window.innerWidth,
    document: document.documentElement.scrollWidth,
    body: document.body.scrollWidth,
  }))
  if (widths.document > widths.viewport || widths.body > widths.viewport) {
    throw new Error(`Page overflows viewport: ${JSON.stringify(widths)}`)
  }
}

export { aggregateFixture, realRateFixture }
