import { defineConfig, devices } from '@playwright/test';
import { existsSync } from 'node:fs';

/**
 * Playwright config for WowRepo's browser + visual checks.
 *
 * The tests build the static site and preview it, then verify each Garden page
 * loads, has no horizontal overflow, navigates, shows critical content, and
 * logs no console errors — across four viewports. Screenshots for all pages are
 * written to tests/visual/screenshots.
 *
 * Environment note: this container ships a pinned Chromium at
 * /opt/pw-browsers/chromium. If present we launch it directly instead of
 * downloading a browser (see docs/mvp-report.md → Known limitations).
 */
const PINNED_CHROMIUM = '/opt/pw-browsers/chromium';
const usePinned = existsSync(PINNED_CHROMIUM);

export default defineConfig({
  testDir: './tests/visual',
  fullyParallel: true,
  forbidOnly: !!process.env.CI,
  retries: process.env.CI ? 1 : 0,
  reporter: [['list'], ['html', { open: 'never' }]],
  outputDir: './test-results',
  timeout: 30_000,
  expect: { timeout: 5_000 },

  use: {
    baseURL: 'http://localhost:4321',
    trace: 'on-first-retry',
    launchOptions: usePinned ? { executablePath: PINNED_CHROMIUM } : {},
  },

  // Build once, then preview the static output for the whole run.
  webServer: {
    command: 'npm run build && npm run preview -- --port 4321 --host',
    url: 'http://localhost:4321',
    reuseExistingServer: !process.env.CI,
    timeout: 120_000,
  },

  projects: [
    {
      name: 'desktop',
      use: { ...devices['Desktop Chrome'], viewport: { width: 1440, height: 900 } },
    },
    {
      name: 'laptop',
      use: { ...devices['Desktop Chrome'], viewport: { width: 1280, height: 800 } },
    },
    {
      name: 'mobile',
      use: { ...devices['Desktop Chrome'], viewport: { width: 390, height: 844 } },
    },
    {
      name: 'mobile-narrow',
      use: { ...devices['Desktop Chrome'], viewport: { width: 320, height: 720 } },
    },
  ],
});
