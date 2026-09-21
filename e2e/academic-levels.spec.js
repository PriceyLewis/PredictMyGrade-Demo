const { test, expect } = require('@playwright/test');

test.describe('academic level feature coverage', () => {
  test('college offer, scenario, personal statement and checklist flows work', async ({ page }) => {
    await page.goto('/college/');
    const stamp = Date.now();
    const institution = `E2E University ${stamp}`;
    const course = `Computer Science ${stamp}`;

    const addForm = page.locator('form[action$="/college/ucas/add/"]');
    await addForm.locator('[name="institution"]').fill(institution);
    await addForm.locator('[name="course"]').fill(course);
    await addForm.locator('[name="points"]').fill('128');
    await addForm.locator('[name="decision_type"]').selectOption('conditional');
    await addForm.locator('[name="target_points"]').fill('144');
    await addForm.locator('[name="notes"]').fill('E2E offer');
    await Promise.all([
      page.waitForURL(/\/college\/$/),
      addForm.getByRole('button', { name: 'Add offer' }).click(),
    ]);

    let row = page.locator('#offerTrackerTable tbody tr', { hasText: institution }).first();
    await expect(row).toBeVisible();

    await page.locator('[data-offer-filter="offer"]').click();
    await expect(row).toBeHidden();
    await page.locator('[data-offer-filter="all"]').click();
    await expect(row).toBeVisible();

    const updateForm = row.locator('form[action*="/update/"]');
    await updateForm.locator('[name="status"]').selectOption('offer');
    await updateForm.locator('[name="decision_type"]').selectOption('unconditional');
    await updateForm.locator('[name="points"]').fill('132');
    await updateForm.locator('[name="target_points"]').fill('148');
    await updateForm.locator('[name="notes"]').fill('E2E updated offer');
    await Promise.all([
      page.waitForURL(/\/college\/$/),
      updateForm.getByRole('button', { name: 'Save changes' }).click(),
    ]);

    row = page.locator('#offerTrackerTable tbody tr', { hasText: institution }).first();
    await expect(row.locator('[name="status"]')).toHaveValue('offer');
    await expect(row.locator('[name="decision_type"]')).toHaveValue('unconditional');
    await expect(row).toContainText('132 pts');

    await page.locator('#scenarioSubject').fill(`Scenario ${stamp}`);
    await page.locator('#scenarioQualification').selectOption('ALEVEL');
    await page.locator('#scenarioGrade').selectOption('A');
    await page.locator('#scenarioPercent').fill('80');
    await page.locator('#addScenarioRow').click();
    await expect(page.locator('#scenarioTable')).toContainText(`Scenario ${stamp}`);
    const [scenarioResponse] = await Promise.all([
      page.waitForResponse(response => response.url().includes('/college/ucas/simulate/') && response.request().method() === 'POST'),
      page.locator('#runScenarioBtn').click(),
    ]);
    expect(scenarioResponse.ok()).toBeTruthy();
    await expect(page.locator('#scenarioSummary')).toContainText(/Recorded:|Predicted:/);

    const statementForm = page.locator('form[action="/college/personal-statement/"]');
    await statementForm.locator('[name="word_count"]').fill('850');
    await statementForm.locator('[name="target"]').fill('4000');
    await statementForm.locator('[name="deadline"]').fill('2026-12-15');
    await Promise.all([
      page.waitForURL(/\/college\/$/),
      statementForm.getByRole('button', { name: 'Update' }).click(),
    ]);
    await expect(page.locator('form[action="/college/personal-statement/"] [name="word_count"]')).toHaveValue('850');

    const checklistForm = page.locator('form[action="/college/super-curricular/toggle/"]').first();
    const checkbox = checklistForm.locator('input[type="checkbox"]');
    const wasChecked = await checkbox.isChecked();
    await Promise.all([
      page.waitForURL(/\/college\/$/),
      checkbox.click(),
    ]);
    const persistedChecklist = page.locator('form[action="/college/super-curricular/toggle/"]').first().locator('input[type="checkbox"]');
    expect(await persistedChecklist.isChecked()).toBe(!wasChecked);

    row = page.locator('#offerTrackerTable tbody tr', { hasText: institution }).first();
    page.once('dialog', dialog => dialog.accept().catch(() => {}));
    await Promise.all([
      page.waitForURL(/\/college\/$/),
      row.getByRole('button', { name: 'Delete' }).click(),
    ]);
    await expect(page.locator('#offerTrackerTable')).not.toContainText(institution);
  });

  test('GCSE revision, past-paper and exam-checklist flows work', async ({ page }) => {
    await page.goto('/gcse/');
    const stamp = Date.now();
    const paperName = `E2E Paper ${stamp}`;

    const revisionForm = page.locator('form[action="/gcse/revision/add/"]');
    await revisionForm.locator('[name="subject"]').selectOption({ index: 1 });
    const subjectText = ((await revisionForm.locator('[name="subject"] option:checked').textContent()) || '').trim();
    await revisionForm.locator('[name="date"]').fill('2026-12-16');
    await revisionForm.locator('[name="time"]').fill('18:30');
    await Promise.all([
      page.waitForURL(/\/gcse\/$/),
      revisionForm.getByRole('button', { name: 'Add session' }).click(),
    ]);
    const revisionItem = page.locator('#revision-block .list li', { hasText: subjectText }).last();
    await expect(revisionItem).toBeVisible();

    const paperForm = page.locator('form[action="/gcse/papers/add/"]');
    await paperForm.locator('[name="name"]').fill(paperName);
    await paperForm.locator('[name="score"]').fill('76.5');
    await paperForm.locator('[name="status"]').selectOption('queued');
    await Promise.all([
      page.waitForURL(/\/gcse\/$/),
      paperForm.getByRole('button', { name: 'Add paper' }).click(),
    ]);

    let paperRow = page.locator('table tbody tr', { hasText: paperName }).first();
    await expect(paperRow).toBeVisible();
    const updatePaper = paperRow.locator('form[action*="/gcse/papers/"][action*="/update/"]');
    await updatePaper.locator('[name="status"]').selectOption('completed');
    await updatePaper.locator('[name="score"]').fill('84.5');
    await Promise.all([
      page.waitForURL(/\/gcse\/$/),
      updatePaper.getByRole('button', { name: 'Save' }).click(),
    ]);
    paperRow = page.locator('table tbody tr', { hasText: paperName }).first();
    await expect(paperRow.locator('[name="status"]')).toHaveValue('completed');
    await expect(paperRow.locator('[name="score"]')).toHaveValue('84.5');

    const checklistForm = page.locator('form[action="/gcse/exam-checklist/toggle/"]').first();
    const checkbox = checklistForm.locator('input[type="checkbox"]');
    const wasChecked = await checkbox.isChecked();
    await Promise.all([
      page.waitForURL(/\/gcse\/$/),
      checkbox.click(),
    ]);
    const persistedChecklist = page.locator('form[action="/gcse/exam-checklist/toggle/"]').first().locator('input[type="checkbox"]');
    expect(await persistedChecklist.isChecked()).toBe(!wasChecked);

    paperRow = page.locator('table tbody tr', { hasText: paperName }).first();
    page.once('dialog', dialog => dialog.accept().catch(() => {}));
    await Promise.all([
      page.waitForURL(/\/gcse\/$/),
      paperRow.getByRole('button', { name: 'Delete' }).click(),
    ]);
    await expect(page.locator('body')).not.toContainText(paperName);

    const refreshedRevisionItem = page.locator('#revision-block .list li', { hasText: subjectText }).last();
    page.once('dialog', dialog => dialog.accept().catch(() => {}));
    await Promise.all([
      page.waitForURL(/\/gcse\/$/),
      refreshedRevisionItem.getByRole('button', { name: 'Delete' }).click(),
    ]);
  });

  test('remaining user-facing academic and history pages render without browser errors', async ({ page }) => {
    const paths = [
      '/compare/levels/',
      '/compare/all-levels/',
      '/timeline/',
      '/snapshot-comparison/',
      '/snapshot/history/',
      '/tools/target-grade/',
      '/predictions/',
      '/what-if/history/',
      '/what-if/basic/',
      '/milestones/',
      '/backup/history/',
      '/privacy/dashboard/',
      '/whats-new/',
      '/study-suggestions/',
      '/smart-insights/',
      '/how-it-works/',
      '/welcome-tour/',
      '/demo-notice/',
      '/cookies/',
      '/legal/disclaimer/',
      '/contact-support/',
    ];

    for (const path of paths) {
      const errors = [];
      const onPageError = error => errors.push(error.message);
      const onConsole = message => {
        if (message.type() === 'error' && !/favicon|ResizeObserver loop/i.test(message.text())) {
          errors.push(message.text());
        }
      };
      page.on('pageerror', onPageError);
      page.on('console', onConsole);
      const response = await page.goto(path, { waitUntil: 'domcontentloaded' });
      expect(response && response.status(), path + ' should return a non-error response').toBeLessThan(400);
      await expect(page.locator('main.page')).toBeVisible();
      await expect(page.locator('body')).not.toContainText(/Internal Server Error|Traceback/i);
      expect(errors, path + ' should not emit browser errors').toEqual([]);
      page.off('pageerror', onPageError);
      page.off('console', onConsole);
    }
  });
});
