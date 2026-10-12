import { defineConfig, devices } from '@playwright/test'

const port = Number(process.env.E2E_PORT ?? '4173')
const baseURL = `http://127.0.0.1:${port}`
const chromiumExecutablePath = process.env.PLAYWRIGHT_CHROMIUM_EXECUTABLE

export default defineConfig({
  testDir: './e2e',
  fullyParallel: false,
  reporter: 'line',
  outputDir: process.env.PLAYWRIGHT_OUTPUT_DIR ?? 'test-results',
  use: {
    baseURL,
    trace: 'retain-on-failure',
  },
  webServer: {
    command: `npm run preview -- --host 127.0.0.1 --port ${port}`,
    url: baseURL,
    reuseExistingServer: false,
  },
  projects: [
    {
      name: 'chromium',
      use: {
        ...devices['Desktop Chrome'],
        launchOptions: chromiumExecutablePath ? { executablePath: chromiumExecutablePath } : undefined,
      },
    },
    { name: 'webkit', use: { ...devices['Desktop Safari'] } },
  ],
})
