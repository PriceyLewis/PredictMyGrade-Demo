const { test, expect } = require('@playwright/test');
const path = require('node:path');

const freeUserStorageState = path.join(__dirname, '.auth', 'free-user.json');

test.describe('portfolio demo flows', () => {
  test('free dashboard remains signed in and does not poll premium insight feeds', async ({ browser, baseURL }) => {
    const context = await browser.newContext({ storageState: freeUserStorageState, baseURL });
    const page = await context.newPage();
    const insightRequests = [];
    page.on('request', request => {
      if (/dashboard\/ai_insights\/?$/.test(new URL(request.url()).pathname)) {
        insightRequests.push(request.url());
      }
    });

    await page.goto('/dashboard/?skip_welcome=1');
    await expect(page).toHaveURL(/\/dashboard\//);
    await expect(page.locator('main.page')).toBeVisible();
    await page.waitForTimeout(2500);
    await expect(page).toHaveURL(/\/dashboard\//);
    await expect(page.getByText('Session expired. Please sign in again.')).toHaveCount(0);
    expect(insightRequests).toEqual([]);

    await context.close();
  });

  test('public sign-in exposes demo access without external providers', async ({ browser, baseURL }) => {
    const context = await browser.newContext({
      baseURL,
      storageState: { cookies: [], origins: [] },
    });
    const page = await context.newPage();

    await page.goto('/accounts/login/');
    await expect(page.getByText('PredictMyGrade · Portfolio demo')).toBeVisible();
    await expect(page.getByRole('button', { name: 'Continue as Demo User' })).toBeVisible();
    await expect(page.getByText(/no registration, payment card, or third-party account/i)).toBeVisible();
    await expect(page.getByText('Continue with Google')).toHaveCount(0);
    await expect(page.getByText('Continue with GitHub')).toHaveCount(0);
    await expect(page.getByText('Continue with Microsoft')).toHaveCount(0);

    await context.close();
  });

  test('reviewer can switch free to premium and back to free with mock billing', async ({ browser, baseURL }) => {
    const context = await browser.newContext({ storageState: freeUserStorageState, baseURL });
    const page = await context.newPage();

    await page.goto('/reports/ai/');
    await expect(page).toHaveURL(/\/upgrade\/$/);

    await page.locator('.js-upgrade-cta[data-plan="monthly"]').click();
    await expect(page).toHaveURL(/\/payment\/success\//);
    await expect(page.locator('body')).toContainText(/demo upgrade complete/i);
    await expect(page.locator('body')).toContainText(/no card was charged/i);

    const premiumResponse = await page.goto('/reports/ai/');
    expect(premiumResponse && premiumResponse.status()).toBeLessThan(400);
    await expect(page).toHaveURL(/\/reports\/ai\/$/);

    await page.goto('/manage-subscription/');
    await expect(page.getByRole('button', { name: 'Switch to Demo Monthly' })).toBeVisible();
    await expect(page.getByRole('button', { name: 'Switch to Demo Yearly' })).toBeVisible();
    const returnToFree = page.getByRole('button', { name: 'Return to Free' });
    await expect(returnToFree).toBeVisible();

    page.once('dialog', (dialog) => dialog.accept());
    await returnToFree.click();
    await expect(page.getByText(/free demo plan/i)).toBeVisible();

    await page.goto('/reports/ai/');
    await expect(page).toHaveURL(/\/upgrade\/$/);

    await context.close();
  });
});
