const { test, expect } = require('@playwright/test');

// Review captures, not pixel baselines: a passing test does not certify visual polish.
// CI uses only the disposable QA accounts created by global-setup.js.
const screens = [
  ['login', '/accounts/login/', false],
  ['dashboard', '/dashboard/?skip_welcome=1', true],
  ['modules', '/modules/', true],
  ['college', '/college/', true],
  ['gcse', '/gcse/', true],
  ['compare-levels', '/compare/levels/', true],
  ['compare-all-levels', '/compare/all-levels/', true],
  ['timeline', '/timeline/', true],
  ['prediction-history', '/predictions/', true],
  ['what-if', '/what-if/', true],
  ['what-if-history', '/what-if/history/', true],
  ['what-if-basic', '/what-if/basic/', true],
  ['target-grade', '/tools/target-grade/', true],
  ['milestones', '/milestones/', true],
  ['smart-insights', '/smart-insights/', true],
  ['privacy', '/privacy/dashboard/', true],
  ['backup-history', '/backup/history/', true],
  ['manage-subscription', '/manage-subscription/', true],
  ['settings', '/settings/', true],
  ['history', '/snapshot/history/', true],
  ['welcome', '/welcome/', true],
  ['welcome-tour', '/welcome-tour/', true],
  ['study-suggestions', '/study-suggestions/', true],
  ['pricing', '/pricing/', true],
  ['upgrade', '/upgrade/', true],
];

for (const viewport of [{ width: 1440, height: 1000 }, { width: 390, height: 844 }]) {
  for (const theme of ['dark', 'light']) {
    test.describe(`${viewport.width}px ${theme}`, () => {
      test.use({ viewport, colorScheme: theme, reducedMotion: 'reduce' });
      for (const [name, url, authenticated] of screens) {
        test(`${name} visual review`, async ({ browser, baseURL }, testInfo) => {
          const context = await browser.newContext({
            baseURL, viewport, colorScheme: theme, reducedMotion: 'reduce',
            storageState: authenticated
              ? (name === 'upgrade' ? './e2e/.auth/free-user.json' : './e2e/.auth/user.json')
              : undefined,
          });
          try {
            await context.addInitScript(selectedTheme => {
              localStorage.setItem('predictmygrade-theme', selectedTheme);
            }, theme);
            const page = await context.newPage();
            const errors = [];
            page.on('pageerror', error => errors.push(error.message));
            page.on('console', message => {
              if (message.type() === 'error' && !/favicon|ResizeObserver loop/i.test(message.text())) {
                errors.push('console: ' + message.text());
              }
            });
            const response = await page.goto(url);
            expect(response.status()).toBe(200);
            expect(new URL(page.url()).pathname).toBe(new URL(url, baseURL).pathname);
            await expect(page.locator('main.page')).toBeVisible();
            if (name === 'upgrade' && viewport.width <= 640) {
              await page.evaluate(() => {
                localStorage.removeItem('predictmygrade.cookieConsent');
                document.cookie = 'pmg_cookie_consent=;path=/;max-age=0;SameSite=Lax';
              });
              await page.reload({ waitUntil: 'domcontentloaded' });
              await expect(page.locator('main.page')).toBeVisible();
              await expect(page.locator('[data-cookie-banner]')).toBeVisible();
            }
            if (name === 'settings') {
              for (const form of await page.locator('.settings-form').all()) {
                const layout = await form.evaluate(element => {
                  const rect = element.getBoundingClientRect();
                  const select = element.querySelector('select').getBoundingClientRect();
                  const button = element.querySelector('button').getBoundingClientRect();
                  const copy = element.querySelector('.settings-form__copy').getBoundingClientRect();
                  const overlap = Math.min(select.right, button.right) > Math.max(select.left, button.left) + 1 &&
                    Math.min(select.bottom, button.bottom) > Math.max(select.top, button.top) + 1;
                  return { overlap, inside: select.left >= rect.left && select.right <= rect.right,
                    copyAbove: copy.bottom <= select.top, width: select.width };
                });
                expect(layout.overlap).toBe(false);
                expect(layout.inside).toBe(true);
                expect(layout.copyAbove).toBe(true);
                expect(layout.width).toBeGreaterThan(150);
              }
            }
            if (name === 'welcome') {
              await expect(page.locator('.feature-card').first()).toContainText('Grade Tracker');
              const readable = await page.locator('.feature-card').first().evaluate(element => {
                const card = getComputedStyle(element);
                const heading = getComputedStyle(element.querySelector('h3'));
                const body = getComputedStyle(element.querySelector('p'));
                return {
                  background: card.backgroundColor,
                  heading: heading.color,
                  body: body.color,
                };
              });
              expect(readable.heading).not.toBe(readable.background);
              expect(readable.body).not.toBe(readable.background);
            }
            if (name === 'modules' && viewport.width <= 640) {
              const modulesTable = page.locator('.modules-table');
              await expect(modulesTable).toBeVisible();
              const tableLayout = await modulesTable.evaluate(element => ({
                width: element.getBoundingClientRect().width,
                scrollWidth: element.scrollWidth,
                display: getComputedStyle(element).display,
              }));
              expect(tableLayout.scrollWidth).toBeLessThanOrEqual(tableLayout.width + 2);
              await expect(page.locator('#moduleBody tr').first().locator('[data-label="Action"]')).toBeVisible();
            }
            if (name === 'college' && viewport.width <= 640) {
              await expect(page.locator('.offer-pill').first()).toBeVisible();
              await expect(page.locator('#offerAlertMessage')).toBeVisible();
              const header = page.locator('.offer-card__header');
              await expect(header).toBeVisible();
              expect(await header.evaluate(element => getComputedStyle(element).display)).toBe('grid');
              for (const selector of ['#ucasPointsTable', '#offerTrackerTable', '#scenarioTable']) {
                const tableLayout = await page.locator(selector).evaluate(element => ({
                  width: element.getBoundingClientRect().width,
                  scrollWidth: element.scrollWidth,
                  minWidth: getComputedStyle(element).minWidth,
                }));
                expect(tableLayout.scrollWidth, selector + ' must fit without horizontal scrolling')
                  .toBeLessThanOrEqual(tableLayout.width + 2);
                expect(tableLayout.minWidth).toBe('0px');
              }
            }
            if (name === 'upgrade') {
              await expect(page.locator('#upgrade-alert')).toBeHidden();
              await expect(page.locator('.plan-card')).toHaveCount(2);
              await expect(page.locator('.plan-card').nth(0)).toBeVisible();
              await expect(page.locator('.plan-card').nth(1)).toBeVisible();
              await expect(page.locator('.comparison-table')).toBeVisible();
              await expect(page.locator('.comparison-table')).toContainText('Advanced analytics');
              if (viewport.width <= 640) {
                const headerStyle = await page.locator('.comparison-table th').last().evaluate(element => ({
                  whiteSpace: getComputedStyle(element).whiteSpace,
                  height: element.getBoundingClientRect().height,
                }));
                expect(headerStyle.whiteSpace).toBe('nowrap');
                expect(headerStyle.height).toBeLessThan(50);

                const cookieBanner = page.locator('[data-cookie-banner]');
                const monthlyCta = page.locator('.js-upgrade-cta[data-plan="monthly"]').first();
                await expect(cookieBanner).toBeVisible();
                await expect(monthlyCta).toBeVisible();
                const [bannerBox, ctaBox] = await Promise.all([
                  cookieBanner.boundingBox(),
                  monthlyCta.boundingBox(),
                ]);
                expect(bannerBox).not.toBeNull();
                expect(ctaBox).not.toBeNull();
                expect(
                  bannerBox.y >= ctaBox.y + ctaBox.height + 4 ||
                  ctaBox.y >= bannerBox.y + bannerBox.height + 4,
                  'Cookie banner must not overlap the monthly upgrade CTA on mobile'
                ).toBeTruthy();
                await page.screenshot({
                  path: testInfo.outputPath(`${name}-${viewport.width}-${theme}-cookie-visible.png`),
                  fullPage: false,
                  animations: 'disabled',
                });
              }
            }
            const consent = page.getByRole('button', { name: 'Only essential', exact: true });
            if (await consent.isVisible()) await consent.click();
            await page.evaluate(() => document.fonts.ready);
            // Let on-scroll content render before the full-page capture.
            await page.evaluate(async () => {
              for (let y = 0; y < document.body.scrollHeight; y += window.innerHeight) {
                window.scrollTo(0, y);
                await new Promise(resolve => requestAnimationFrame(() => requestAnimationFrame(resolve)));
              }
              window.scrollTo(0, 0);
            });
            await page.screenshot({ path: testInfo.outputPath(`${name}-${viewport.width}-${theme}.png`), fullPage: true, animations: 'disabled' });
            const layout = await page.evaluate(() => ({
              viewport: window.innerWidth,
              documentWidth: document.documentElement.scrollWidth,
              overflowing: [...document.querySelectorAll('main *')].filter(element => {
                const rect = element.getBoundingClientRect();
                return rect.width > 0 && (rect.right > window.innerWidth + 1 || rect.left < -1)
                  && !element.closest('.table-responsive');
              }).slice(0, 20).map(element => ({ tag: element.tagName, class: element.className })),
            }));
            await testInfo.attach('layout', { body: JSON.stringify({ ...layout, errors }, null, 2), contentType: 'application/json' });
            if (layout.documentWidth > viewport.width + 1 || errors.length) {
              console.log('VISUAL_DIAGNOSTIC', testInfo.titlePath.join(' / '), JSON.stringify({ ...layout, errors }));
            }
            expect.soft(layout.documentWidth, 'Page should not scroll horizontally').toBeLessThanOrEqual(viewport.width + 1);
            expect.soft(errors, 'Uncaught browser errors').toEqual([]);
          } finally {
            await context.close();
          }
        });
      }
    });
  }
}
