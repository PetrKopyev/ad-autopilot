import { defineConfig } from '@playwright/test';

export default defineConfig({
  testDir: '.',
  testMatch: 'flow.spec.ts',
  timeout: 300_000,
  expect: { timeout: 90_000 },
  workers: 1,
  outputDir: '/tmp/ad-autopilot-e2e',
  reporter: 'list',
  use: {
    baseURL: process.env.BASE_URL || 'https://localhost',
    ignoreHTTPSErrors: true,
    browserName: 'chromium',
    launchOptions: process.env.CHROMIUM_PATH ? { executablePath: process.env.CHROMIUM_PATH } : {},
  },
});
