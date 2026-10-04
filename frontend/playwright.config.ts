import { defineConfig, devices } from '@playwright/test'

/** 新增布局项目仅匹配全局无障碍／布局验收，既有含数据库的用户流程保持只运行一次。 */
const presentationSpecs = /(?:accessibility|layout)\.spec\.ts$/
export default defineConfig({
  testDir: './e2e',
  fullyParallel: false,
  retries: process.env.CI ? 2 : 0,
  reporter: 'html',
  use: {
    baseURL: 'http://127.0.0.1:5173',
    trace: 'on-first-retry',
    screenshot: 'only-on-failure',
  },
  projects: [
    {
      name: 'chromium',
      use: {
        ...devices['Desktop Chrome'],
        viewport: { width: 1400, height: 900 },
      },
    },
    {
      name: 'tablet',
      testMatch: presentationSpecs,
      use: {
        ...devices['Desktop Chrome'],
        viewport: { width: 900, height: 1000 },
      },
    },
    {
      name: 'mobile',
      testMatch: presentationSpecs,
      use: {
        ...devices['Desktop Chrome'],
        viewport: { width: 375, height: 812 },
      },
    },
  ],
  webServer: [
    {
      command: 'bash ../scripts/run-e2e-backend.sh',
      url: 'http://127.0.0.1:8000/api/v1/system/health',
      reuseExistingServer: !process.env.CI,
      timeout: 120_000,
      gracefulShutdown: { signal: 'SIGTERM', timeout: 45_000 },
    },
    {
      command: 'pnpm dev --host 127.0.0.1',
      url: 'http://127.0.0.1:5173',
      reuseExistingServer: !process.env.CI,
      timeout: 120_000,
    },
  ],
})
