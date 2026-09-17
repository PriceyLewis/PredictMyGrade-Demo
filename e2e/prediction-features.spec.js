const { test, expect } = require('@playwright/test');
const fs = require('node:fs/promises');

test('What-If submits scenarios, renders results and exports usable files', async ({ page }) => {
  const errors = [];
  page.on('pageerror', error => errors.push(error.message));
  await page.goto('/what-if/');
  await page.locator('[name="sim_name"]').fill('QA Scenario');
  await page.locator('[name="sim_mark"]').fill('75');
  await page.locator('[name="sim_credits"]').fill('20');
  await page.locator('#studyStart').fill('2027-01-15');
  const response = page.waitForResponse(r => r.url().includes('/api/predict_what_if/') && r.request().method() === 'POST');
  await page.locator('#simForm button[type="submit"]').click();
  expect((await response).ok()).toBeTruthy();
  await expect(page.locator('#scenarioTableBody')).toContainText('QA Scenario');
  await expect(page.locator('#chartCard')).toBeVisible();
  for (const [id, extension] of [['exportPlanJson', 'json'], ['exportPlanIcs', 'ics']]) {
    const pending = page.waitForEvent('download');
    await page.locator(`#${id}`).click();
    const download = await pending;
    const content = await fs.readFile(await download.path(), 'utf8');
    expect(download.suggestedFilename()).toMatch(new RegExp(`\\.${extension}$`));
    if (extension === 'json') expect(JSON.parse(content).scenarios.length).toBeGreaterThan(0);
    else {
      expect(content).toMatch(/^BEGIN:VCALENDAR\r\n/);
      expect(content).toContain('\r\nBEGIN:VEVENT\r\n');
      expect(content).toMatch(/END:VCALENDAR\r\n$/);
    }
  }
  expect(errors).toEqual([]);
});
