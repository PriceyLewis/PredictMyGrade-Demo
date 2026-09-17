const { test, expect } = require('@playwright/test');
const path = require('node:path');

const freeUserStorageState = path.join(__dirname, '.auth', 'free-user.json');
const upgradeUserStorageState = path.join(__dirname, '.auth', 'upgrade-user.json');
const adminUserStorageState = path.join(__dirname, '.auth', 'admin-user.json');

async function seedDeadline(page, title, options = {}) {
  return page.evaluate(async ({ title, options }) => {
    const csrf =
      document.querySelector('#studyPlanForm [name="csrfmiddlewaretoken"]')?.value ||
      document.querySelector('[name="csrfmiddlewaretoken"]')?.value ||
      '';
    const dueDate = new Date();
    dueDate.setDate(dueDate.getDate() + (options.daysFromNow ?? 3));
    const payload = [
      {
        title,
        due_date: dueDate.toISOString().slice(0, 10),
        weight: options.weight ?? 1.5,
        module: options.module ?? 'QA Module',
      },
    ];
    const response = await fetch('/save_upcoming_deadlines/', {
      method: 'POST',
      headers: {
        'Content-Type': 'application/json',
        'X-CSRFToken': csrf,
      },
      body: JSON.stringify(payload),
    });
    const data = await response.json();
    return { ok: response.ok, data };
  }, { title, options });
}

test.describe('core smoke flows', () => {
  test('public pages load', async ({ page }) => {
    for (const path of ['/help/', '/privacy/', '/terms/']) {
      await page.goto(path);
      await expect(page).toHaveURL(new RegExp(path.replace(/[.*+?^${}()|[\]\\]/g, '\\$&')));
      await expect(page.locator('body')).toBeVisible();
    }
  });

  test('dashboard render for authenticated user', async ({ page }) => {
    await page.goto('/dashboard/');
    await expect(page.getByText(/PredictMyGrade/i).first()).toBeVisible();
    await expect(page.locator('#avgDisplay')).toBeVisible();
    await expect(page.locator('#moduleLibraryCard')).toBeVisible();
  });

  test('modules page can add, search, edit and delete a module', async ({ page }) => {
    await page.goto('/modules/');
    await page.waitForLoadState('networkidle');
    const uniqueName = `E2E Module ${Date.now()}`;
    await page.locator('#addForm input[name="name"]').fill(uniqueName);
    await page.locator('#addForm select[name="level"]').selectOption('UNI');
    await page.locator('#addForm input[name="credits"]').fill('15');
    await page.locator('#addForm input[name="grade_percent"]').fill('67');
    await Promise.all([
      page.waitForEvent('load'),
      page.locator('#addForm button[type="submit"]').click(),
    ]);
    const row = page.locator('#moduleBody tr').filter({ has: page.locator(`input[data-field="name"][value="${uniqueName}"]`) });
    await expect(row.locator('[data-field="name"]')).toHaveValue(uniqueName);
    await page.locator('#searchModules').fill('no-matching-module-name');
    await expect(row).toBeHidden();
    await expect(page.locator('#emptyModules')).toContainText('No modules match');
    await page.locator('#searchModules').fill(uniqueName);
    await expect(row).toBeVisible();
    await row.locator('[data-field="grade_percent"]').fill('75');
    await Promise.all([
      page.waitForResponse(response => response.url().includes('/modules/update/') && response.request().method() === 'POST'),
      row.locator('[data-field="grade_percent"]').press('Tab'),
    ]);
    await expect(row.locator('.module-status')).toHaveText('Saved');
    await page.reload();
    await expect(row.locator('[data-field="grade_percent"]')).toHaveValue('75.0');

    const deleteButton = row.locator('form.delete-form button[type="submit"]');
    page.once('dialog', (dialog) => dialog.accept().catch(() => {}));
    await Promise.all([
      page.waitForResponse(response => response.url().includes('/modules/delete/') && response.request().method() === 'POST'),
      deleteButton.click(),
    ]);
    await expect(row).toHaveCount(0);
  });

  test('settings page updates theme preference', async ({ page }) => {
    await page.goto('/settings/');
    await page.locator('#themeSelect').selectOption('dark');
    await page.locator('form[action="/settings/update/"] button[type="submit"]').first().click();
    await expect(page.locator('body')).toBeVisible();
  });

  test('mock upgrade flow reaches success page', async ({ browser, baseURL }) => {
    const context = await browser.newContext({ storageState: upgradeUserStorageState, baseURL });
    const page = await context.newPage();
    await page.goto('/upgrade/');
    await page.locator('.js-upgrade-cta[data-plan="monthly"]').click();
    await expect(page).toHaveURL(/payment\/success/);
    await expect(page.locator('body')).toContainText(/premium|success|activated/i);
    await context.close();
  });

  test('authenticated mock pages load', async ({ page }) => {
    for (const path of [
      '/what-if/',
      '/reports/ai/',
      '/settings/',
      '/manage-subscription/',
      '/college/',
      '/gcse/',
      '/smart-insights/',
    ]) {
      const response = await page.goto(path);
      expect(response && response.status()).toBeLessThan(400);
    }

    const [download] = await Promise.all([
      page.waitForEvent('download'),
      page.evaluate(() => {
        window.location.href = '/dashboard/study-plan/calendar/';
      }),
    ]);
    expect(download.suggestedFilename()).toMatch(/study-plan.*\.ics/i);
  });

  test('can create a goal from the dashboard', async ({ page }) => {
    await page.goto('/dashboard/');
    const goalTitle = `E2E Goal ${Date.now()}`;
    await page.locator('#goalText').fill(goalTitle);
    await page.locator('#goalCategory').selectOption('academic');
    await page.locator('#goalTarget').fill('68');
    await page.locator('[data-goal-submit]').click();
    await expect(page.locator('#goalList')).toContainText(goalTitle);
  });

  test('can add a manual study plan item', async ({ page }) => {
    await page.goto('/dashboard/');
    const title = `E2E Session ${Date.now()}`;
    await page.locator('#studyPlanForm input[name="title"]').fill(title);
    await page.locator('#studyPlanForm input[name="duration_hours"]').fill('1.25');
    await page.locator('#studyPlanForm button[type="submit"]').click();
    const [download] = await Promise.all([
      page.waitForEvent('download'),
      page.evaluate(() => {
        window.location.href = '/dashboard/study-plan/calendar/';
      }),
    ]);
    expect(download.suggestedFilename()).toMatch(/study-plan.*\.ics/i);
  });

  test('deadline actions work from the dashboard UI', async ({ page }) => {
    await page.goto('/dashboard/');
    const originalTitle = `E2E Deadline ${Date.now()}`;
    const updatedTitle = `${originalTitle} Updated`;
    const seedResult = await seedDeadline(page, originalTitle, { weight: 2.5, module: 'QA Module' });
    expect(seedResult.ok).toBeTruthy();
    expect(seedResult.data.ok).toBeTruthy();

    await page.reload();
    const row = page.locator('#dashboardDeadlines tbody tr', { hasText: originalTitle }).first();
    await expect(row).toBeVisible();

    page.once('dialog', (dialog) => dialog.accept(updatedTitle).catch(() => {}));
    await row.locator('.inline-edit-deadline[data-field="title"]').click();
    await expect(row.locator('.deadline-title__text')).toHaveText(updatedTitle);

    page.once('dialog', (dialog) => dialog.accept('4.5').catch(() => {}));
    await row.locator('.inline-edit-deadline[data-field="weight"]').click();
    await expect(row.locator('.deadline-weight')).toHaveText(/4.5/);

    const dueBefore = (await row.locator('.deadline-date').textContent())?.trim();
    await row.locator('.deadline-snooze').click();
    await expect(row.locator('.deadline-date')).not.toHaveText(dueBefore || '');

    page.once('dialog', (dialog) => dialog.accept().catch(() => {}));
    const [moveResponse] = await Promise.all([
      page.waitForResponse((response) => response.url().includes('/move_to_plan/') && response.request().method() === 'POST'),
      row.locator('.deadline-move').click(),
    ]);
    const movePayload = await moveResponse.json();
    expect(movePayload.ok).toBeTruthy();
    expect(movePayload.plan_item.title).toContain(updatedTitle);
    await expect(page.locator('#weeklyCalendar')).toContainText(new RegExp(updatedTitle.replace(/[.*+?^${}()|[\]\\]/g, '\\$&')));

    await row.locator('.mark-complete').click();
    await expect(row).toHaveClass(/completed/);
    await expect(row.locator('.mark-complete')).toBeDisabled();
  });

  test('AI study plan generation populates the planner calendar', async ({ page }) => {
    await page.goto('/dashboard/');
    const deadlineTitle = `AI Plan Deadline ${Date.now()}`;
    const seedResult = await seedDeadline(page, deadlineTitle, { weight: 3, module: 'QA Module' });
    expect(seedResult.ok).toBeTruthy();
    expect(seedResult.data.ok).toBeTruthy();

    const [planResponse] = await Promise.all([
      page.waitForResponse((response) => response.url().includes('/dashboard/ai_generate_plan/') && response.request().method() === 'GET'),
      page.locator('#aiGeneratePlanBtn').click(),
    ]);
    const planPayload = await planResponse.json();
    expect(planPayload.msg).toMatch(/plan generated/i);
    expect(planPayload.plan_items).toHaveLength(7);
    await expect(page.locator('#weeklyCalendar')).toContainText(/Sprint:|Progress|General revision/);
    await expect(page.locator('#weeklyCalendarHint')).toHaveText(/Synced with AI assistant and planner/i);
  });

  test('premium assistant can send a message', async ({ page }) => {
    await page.goto('/dashboard/');
    const result = await page.evaluate(async () => {
      const response = await fetch('/dashboard/assistant/chat/', {
        method: 'POST',
        headers: {
          'Content-Type': 'application/json',
          'X-CSRFToken': document.querySelector('[name="csrfmiddlewaretoken"]')?.value || '',
        },
        body: JSON.stringify({ message: 'How am I doing today?' }),
      });
      const data = await response.json();
      return { ok: response.ok, data };
    });
    expect(result.ok).toBeTruthy();
    expect(result.data.ok).toBeTruthy();
    expect(result.data.answer || result.data.history?.length).toBeTruthy();
  });

  test('free user sees assistant upgrade lock', async ({ browser, baseURL }) => {
    const context = await browser.newContext({ storageState: freeUserStorageState, baseURL });
    const page = await context.newPage();
    await page.goto('/dashboard/?skip_welcome=1');
    await expect(page.locator('#assistantPremiumFeature')).toContainText(/upgrade|free users get a daily mentor preview/i);
    await context.close();
  });

  test('what-if prediction endpoint returns results for browser session', async ({ page }) => {
    await page.goto('/what-if/');
    const result = await page.evaluate(async () => {
      const csrf = document.querySelector('[name="csrfmiddlewaretoken"]')?.value || '';
      const response = await fetch('/api/predict_what_if/', {
        method: 'POST',
        headers: {
          'Content-Type': 'application/json',
          'X-CSRFToken': csrf,
        },
        body: JSON.stringify({
          target_avg: 70,
          study_hours: 4,
          plan_weeks: 4,
          sims: [{ name: 'Stretch', mark: 75, credits: 20 }],
        }),
      });
      const data = await response.json();
      return { ok: response.ok, data };
    });
    expect(result.ok).toBeTruthy();
    expect(result.data.ok).toBeTruthy();
    expect(result.data.predicted_points.length).toBeGreaterThan(0);
  });

  test('admin hub requires admin and loads for superuser', async ({ page, browser, baseURL }) => {
    const nonAdminResponse = await page.goto('/admin/hub/');
    expect(nonAdminResponse && nonAdminResponse.status()).toBeLessThan(400);
    await expect(page).toHaveURL(/\/$|\/dashboard\//);

    const context = await browser.newContext({ storageState: adminUserStorageState, baseURL });
    const adminPage = await context.newPage();
    await adminPage.goto('/admin/hub/');
    await expect(adminPage.locator('main .title')).toHaveText(/Admin Hub/i);
    await expect(adminPage.getByText(/Manage premium access/i)).toBeVisible();
    await context.close();
  });

  test('admin can toggle mock premium access from the user manager', async ({ browser, baseURL }) => {
    const context = await browser.newContext({ storageState: adminUserStorageState, baseURL });
    const page = await context.newPage();
    await page.goto('/admin/users/');

    const search = page.locator('#userSearch');
    await search.fill('qatoggle');
    const row = page.locator('tbody tr', { hasText: 'qatoggle' }).first();
    await expect(row).toBeVisible();

    await row.locator('.toggle-premium-btn').click();
    await expect(row).toContainText(/Premium/);
    await expect(row.locator('.toggle-premium-btn')).toHaveText(/Set free/i);

    await row.locator('.toggle-premium-btn').click();
    await expect(row).toContainText(/Free/);
    await expect(row.locator('.toggle-premium-btn')).toHaveText(/Make premium/i);

    await context.close();
  });
});
