const { test, expect } = require('@playwright/test');
const fs = require('node:fs/promises');

test.describe('settings feature contracts', () => {
  test('saved theme changes appearance, survives reload and navigation', async ({ page }) => {
    await page.goto('/settings/');
    for (const theme of ['light', 'dark']) {
      await page.locator('#themeSelect').selectOption(theme);
      await page.getByRole('button', { name: 'Save theme', exact: true }).click();
      await expect(page.locator('.page-notices')).toContainText(`Theme set to ${theme === 'light' ? 'Light' : 'Dark'} mode`);
      await expect(page.locator('html')).toHaveClass(`theme-${theme}`);
      await page.reload();
      await expect(page.locator('#themeSelect')).toHaveValue(theme);
      await expect(page.locator('html')).toHaveClass(`theme-${theme}`);
      await page.goto('/modules/');
      await expect(page.locator('html')).toHaveClass(`theme-${theme}`);
      await page.goto('/settings/');
    }
  });

  test('navigation toggle and account preference stay in sync on mobile', async ({ page }) => {
    await page.setViewportSize({ width: 390, height: 844 });
    await page.goto('/settings/');
    const before = await page.locator('#themeSelect').inputValue();
    await page.locator('#siteNavToggle').click();
    const toggle = page.locator('.mobile-only [data-theme-toggle]');
    await expect(toggle).toBeVisible();
    const saved = page.waitForResponse(r => r.url().includes('/settings/update/') && r.request().method() === 'POST');
    await toggle.click();
    expect((await saved).ok()).toBeTruthy();
    await page.reload();
    const after = before === 'dark' ? 'light' : 'dark';
    await expect(page.locator('#themeSelect')).toHaveValue(after);
    await expect(page.locator('html')).toHaveClass(`theme-${after}`);
  });

  test('persona description and saved selection persist', async ({ page }) => {
    await page.goto('/settings/');
    await page.locator('#settingsPersona').selectOption('coach');
    const expected = await page.locator('#settingsPersona option:checked').getAttribute('data-description');
    await expect(page.locator('#settingsPersonaDescription')).toHaveText(expected);
    await page.getByRole('button', { name: 'Save persona', exact: true }).click();
    await expect(page.locator('.page-notices')).toContainText('AI assistant persona updated');
    await page.reload();
    await expect(page.locator('#settingsPersona')).toHaveValue('coach');
  });

  test('celebrations can be disabled and enabled', async ({ page }) => {
    await page.goto('/settings/');
    for (const enabled of [false, true]) {
      await page.locator('#milestoneEffectsToggle').setChecked(enabled);
      await page.getByRole('button', { name: 'Update milestone settings', exact: true }).click();
      await expect(page.locator('.page-notices')).toBeVisible();
      await page.reload();
      if (enabled) await expect(page.locator('#milestoneEffectsToggle')).toBeChecked();
      else await expect(page.locator('#milestoneEffectsToggle')).not.toBeChecked();
    }
  });

  test('CSV and JSON downloads contain the users data', async ({ page }) => {
    await page.goto('/settings/');
    for (const [label, extension] of [['Export CSV', 'csv'], ['Download data', 'json']]) {
      const downloaded = page.waitForEvent('download');
      await page.getByRole('link', { name: label, exact: true }).click();
      const download = await downloaded;
      expect(download.suggestedFilename()).toMatch(new RegExp(`\\.${extension}$`));
      const content = await fs.readFile(await download.path(), 'utf8');
      if (extension === 'json') expect(JSON.parse(content).user.username).toBe('qauser');
      else expect(content).toContain('QA Module');
    }
    await page.getByRole('link', { name: 'Open dashboard', exact: true }).click();
    await expect(page.locator('main')).toContainText('JSON');
  });

  test('feedback submission shows success using the test-only email backend', async ({ page }) => {
    await page.goto('/settings/');
    const form = page.locator('form').filter({ has: page.locator('input[name="category"][value="feedback"]') });
    await form.locator('[name="subject"]').fill('QA feedback');
    await form.locator('[name="topic"]').selectOption('Feature request');
    await form.locator('[name="message"]').fill('Disposable test feedback; no external email delivery.');
    await form.getByRole('button', { name: 'Send message' }).click();
    await expect(page.locator('.page-notices')).toContainText('Your message has been submitted');
  });

  test('bug report accepts a screenshot and shows success', async ({ page }) => {
    await page.goto('/settings/');
    const form = page.locator('form').filter({ has: page.locator('input[name="category"][value="bug"]') });
    await form.locator('[name="subject"]').fill('QA screenshot report');
    await form.locator('[name="severity"]').selectOption('Low');
    await form.locator('[name="message"]').fill('Disposable browser test.');
    await form.locator('[name="screenshot"]').setInputFiles({ name: 'qa.png', mimeType: 'image/png', buffer: await page.screenshot() });
    await form.getByRole('button', { name: 'Send bug report' }).click();
    await expect(page.locator('.page-notices')).toContainText('Your message has been submitted');
  });

  test('settings links work and delete-account Cancel preserves the session', async ({ page }) => {
    for (const name of ['Help center', "What's new", 'How predictions work', 'Manage subscription']) {
      await page.goto('/settings/');
      await page.getByRole('link', { name, exact: true }).click();
      await expect(page.locator('main')).toBeVisible();
      await expect(page).not.toHaveURL(/accounts\/login/);
    }
    await page.goto('/settings/');
    await page.locator('main').getByRole('link', { name: 'Delete account', exact: true }).click();
    await expect(page.getByRole('heading', { name: 'Confirm account deletion' })).toBeVisible();
    await page.getByRole('link', { name: 'Cancel', exact: true }).click();
    await expect(page).toHaveURL(/\/settings\/$/);
    await expect(page.locator('main')).toContainText('qauser');
  });

  test('AI Assistant navigation opens the panel, not a POST-only endpoint', async ({ page }) => {
    await page.goto('/settings/');
    await page.locator('.site-nav__actions').getByRole('link', { name: 'AI Assistant', exact: true }).click();
    await expect(page).toHaveURL(/#aiAssistantPanel$/);
    await expect(page.locator('#aiAssistantPanel')).toBeVisible();
  });

  test('cookie preferences persist and can be changed', async ({ page }) => {
    await page.goto('/settings/');
    for (const enabled of [true, false]) {
      await page.locator('[data-cookie-open]').click();
      await expect(page.locator('[data-cookie-modal]')).toBeVisible();
      await page.locator('[data-cookie-analytics]').setChecked(enabled);
      await page.locator('[data-cookie-save]').click();
      await expect(page.locator('[data-cookie-modal]')).toBeHidden();
      await page.reload();
      await page.locator('[data-cookie-open]').click();
      if (enabled) await expect(page.locator('[data-cookie-analytics]')).toBeChecked();
      else await expect(page.locator('[data-cookie-analytics]')).not.toBeChecked();
      await page.locator('[data-cookie-modal]').getByRole('button', { name: 'Close', exact: true }).click();
    }
  });
});
