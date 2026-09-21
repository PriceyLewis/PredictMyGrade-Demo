const { test, expect } = require('@playwright/test');
const path = require('node:path');

const adminStorageState = path.join(__dirname, '.auth', 'admin-user.json');

const userPages = [
  '/dashboard/?skip_welcome=1',
  '/welcome/',
  '/modules/',
  '/college/',
  '/gcse/',
  '/compare/levels/',
  '/compare/all-levels/',
  '/timeline/',
  '/progress/timeline/',
  '/reports/ai/',
  '/smart-insights/',
  '/predictions/',
  '/what-if/',
  '/what-if/history/',
  '/what-if/basic/',
  '/tools/target-grade/',
  '/milestones/',
  '/pricing/',
  '/manage-subscription/',
  '/snapshot/history/',
  '/snapshot-comparison/',
  '/privacy/dashboard/',
  '/settings/',
  '/settings/view/',
  '/whats-new/',
  '/contact-support/',
];

test.describe('feature page coverage', () => {
  test('every major authenticated feature page renders without server or browser errors', async ({ page }) => {
    const browserErrors = [];
    page.on('pageerror', error => browserErrors.push(error.message));
    page.on('console', message => {
      if (message.type() === 'error' && !/favicon|ResizeObserver loop/i.test(message.text())) {
        browserErrors.push('console: ' + message.text());
      }
    });

    for (const route of userPages) {
      const response = await page.goto(route, { waitUntil: 'domcontentloaded' });
      expect(response, route + ' should return a response').not.toBeNull();
      expect(response.status(), route + ' should not return an HTTP error').toBeLessThan(400);
      await expect(page.locator('main.page')).toBeVisible();
      const body = await page.locator('body').innerText();
      expect(body, route + ' should not render a server error').not.toMatch(/internal server error|traceback|server error \(500\)/i);
    }

    expect(browserErrors).toEqual([]);
  });

  test('admin feature pages render for a superuser', async ({ browser, baseURL }) => {
    const context = await browser.newContext({ baseURL, storageState: adminStorageState });
    const page = await context.newPage();
    try {
      for (const route of ['/admin/hub/', '/admin/analytics/', '/admin/billing-expiring/', '/admin/system-health/', '/admin/users/']) {
        const response = await page.goto(route, { waitUntil: 'domcontentloaded' });
        expect(response, route + ' should return a response').not.toBeNull();
        expect(response.status(), route + ' should not return an HTTP error').toBeLessThan(400);
        await expect(page.locator('main.page')).toBeVisible();
        await expect(page).not.toHaveURL(/accounts\/login/);
      }
    } finally {
      await context.close();
    }
  });

  test('college offer filters target the offer tracker rows', async ({ page }) => {
    await page.goto('/college/');

    const addForm = page.locator('form[action$="/college/ucas/add/"]');
    const uniqueInstitution = 'QA University ' + Date.now();
    await addForm.locator('[name="institution"]').fill(uniqueInstitution);
    await addForm.locator('[name="course"]').fill('Computer Science');
    await addForm.locator('[name="points"]').fill('120');
    await addForm.locator('[name="target_points"]').fill('128');
    await Promise.all([
      page.waitForLoadState('domcontentloaded'),
      addForm.getByRole('button', { name: 'Add offer' }).click(),
    ]);

    const row = page.locator('#offerTrackerTable tbody tr', { hasText: uniqueInstitution }).first();
    await expect(row).toBeVisible();
    const status = await row.getAttribute('data-status');
    expect(status).toBeTruthy();

    const filter = page.locator('[data-offer-filter="' + status + '"]');
    await expect(filter).toBeVisible();
    await filter.click();
    await expect(row).toBeVisible();
    await expect(page.locator('#offerAlertMessage')).toContainText(/match/i);

    page.once('dialog', dialog => dialog.accept().catch(() => {}));
    await row.locator('form[action*="/delete/"] button[type="submit"]').click();
    await expect(page.locator('#offerTrackerTable')).not.toContainText(uniqueInstitution);
  });

  test('AI prediction endpoint returns a usable prediction payload', async ({ page }) => {
    const response = await page.request.get('/ai/predict/');
    expect(response.status()).toBeLessThan(400);
    const payload = await response.json();
    expect(payload).toEqual(expect.objectContaining({
      predicted_average: expect.any(Number),
      classification: expect.any(String),
      confidence: expect.any(Number),
      model: expect.any(String),
    }));
  });

  test('welcome tour navigation works from first step through finish', async ({ page }) => {
    await page.goto('/welcome-tour/');
    await expect(page.locator('#step-1')).toBeVisible();
    await expect(page.locator('#step-2')).toBeHidden();

    await page.getByRole('button', { name: 'Next →' }).click();
    await expect(page.locator('#step-2')).toBeVisible();
    await page.getByRole('button', { name: '← Back' }).click();
    await expect(page.locator('#step-1')).toBeVisible();

    for (let step = 2; step <= 5; step += 1) {
      await page.getByRole('button', { name: /Next|Finish/ }).click();
      await expect(page.locator('#step-' + step)).toBeVisible();
    }

    await expect(page.getByRole('button', { name: 'Finish' })).toBeVisible();
    await page.getByRole('button', { name: 'Finish' }).click();
    await expect(page).toHaveURL(/\/dashboard\/\?skip_welcome=1$/);
  });

  test('target grade calculator returns a result in the browser', async ({ page }) => {
    await page.goto('/tools/target-grade/');
    await page.locator('[name="current_avg"]').fill('68');
    await page.locator('[name="completed_credits"]').fill('80');
    await page.locator('[name="target_class"]').selectOption('First');
    await page.getByRole('button', { name: 'Calculate Required Grades' }).click();
    await expect(page.getByRole('heading', { name: /Result/ })).toBeVisible();
    await expect(page.locator('main')).toContainText('remaining');
  });
});
