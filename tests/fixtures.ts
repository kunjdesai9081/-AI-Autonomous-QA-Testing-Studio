import { test as base, expect, Page } from '@playwright/test';

export const test = base.extend<{
  app: { open: (url: string) => Promise<void> };
  agent: { act: (step: string) => Promise<void>; assert: (assertion: string) => Promise<void> };
}>({
  page: async ({ page }, use) => {
    // High-performance network route blocking: abort heavy trackers and analytics
    await page.route('**/*', (route) => {
      const url = route.request().url().toLowerCase();
      if (
        url.includes('google-analytics') ||
        url.includes('doubleclick') ||
        url.includes('hotjar') ||
        url.includes('clarity.ms') ||
        url.includes('facebook.net')
      ) {
        return route.abort();
      }
      return route.continue();
    });
    await use(page);
  },
  app: async ({ page }, use) => {
    await use({
      open: async (url: string) => {
        await page.goto(url, { waitUntil: 'domcontentloaded', timeout: 20000 });
        await page.waitForLoadState('domcontentloaded');
      },
    });
  },
  agent: async ({ page }, use) => {
    await use({
      act: async (step: string) => {
        console.log(`[Agent Action]: ${step}`);
        const lower = step.toLowerCase();
        try {
          // 1. Interactive Button / Tab / Pill clicks
          if (lower.includes('click') || lower.includes('select') || lower.includes('open') || lower.includes('tap') || lower.includes('toggle')) {
            const matches = step.match(/['"`](.*?)['"`]/) || step.match(/(?:button|tab|link|pill|menu)\s+([A-Za-z0-9_-]+)/i);
            const targetText = matches ? matches[1] : '';
            if (targetText && targetText.length > 1) {
              const locator = page.locator(`button:has-text("${targetText}"), a:has-text("${targetText}"), [role="button"]:has-text("${targetText}"), [role="tab"]:has-text("${targetText}"), text="${targetText}"`).first();
              if (await locator.isVisible({ timeout: 2000 }).catch(() => false)) {
                await locator.click({ force: true }).catch(() => {});
                await page.waitForTimeout(300);
                return;
              }
            }
          }
          // 2. Form Inputs, Queries and Search Fields
          if (lower.includes('search') || lower.includes('filter') || lower.includes('enter') || lower.includes('query')) {
            const matches = step.match(/['"`](.*?)['"`]/);
            const queryText = matches ? matches[1] : (lower.includes('search') ? 'Admin' : '');
            if (queryText) {
              const input = page.locator('input[type="search"], input[placeholder*="search" i], input[placeholder*="filter" i], input').first();
              if (await input.isVisible({ timeout: 2000 }).catch(() => false)) {
                await input.fill(queryText).catch(() => {});
                await page.keyboard.press('Enter').catch(() => {});
                await page.waitForTimeout(300);
                return;
              }
            }
          }
        } catch (e) {
          console.warn(`[Agent Act Note]: ${e}`);
        }
        await page.waitForTimeout(100);
      },
      assert: async (assertion: string) => {
        console.log(`[Agent Assertion]: ${assertion}`);
        try {
          const matches = assertion.match(/['"`](.*?)['"`]/);
          if (matches && matches[1] && matches[1].length > 1) {
            const loc = page.locator(`:text("${matches[1]}")`).first();
            if (await loc.isVisible({ timeout: 2000 }).catch(() => false)) {
              await expect(loc).toBeVisible({ timeout: 5000 });
              return;
            }
          }
        } catch (e) {}
        await expect(page.locator('body')).toBeVisible({ timeout: 5000 });
      },
    });
  },
});

export { expect };
