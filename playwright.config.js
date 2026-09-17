const { defineConfig, devices } = require('@playwright/test');
const path = require('node:path');

const baseURL = process.env.PLAYWRIGHT_BASE_URL || 'http://127.0.0.1:8001';
const sqlitePath = path.resolve(__dirname, 'db.sqlite3').replace(/\\/g, '/');

module.exports = defineConfig({
  testDir: './e2e',
  globalSetup: './e2e/global-setup.js',
  timeout: 60_000,
  expect: {
    timeout: 10_000,
  },
  fullyParallel: false,
  reporter: 'list',
  use: {
    baseURL,
    storageState: './e2e/.auth/user.json',
    trace: 'retain-on-failure',
    screenshot: 'only-on-failure',
    video: 'retain-on-failure',
  },
  webServer: {
    command: 'python manage.py migrate && python manage.py runserver 127.0.0.1:8001',
    env: {
      ...process.env,
      DJANGO_SECRET_KEY: 'dev-secret-key',
      DATABASE_URL: `sqlite:///${sqlitePath}`,
      DJANGO_DEBUG: 'true',
      DJANGO_ALLOWED_HOSTS: '127.0.0.1,localhost,testserver',
      BILLING_MOCK_MODE: '1',
    },
    url: baseURL,
    reuseExistingServer: true,
    timeout: 120_000,
  },
  projects: [
    {
      name: 'chromium',
      use: { ...devices['Desktop Chrome'] },
    },
  ],
});
