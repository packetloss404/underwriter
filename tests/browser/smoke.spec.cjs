'use strict';
const { test, expect } = require('@playwright/test');

// Read-only deployment smoke; it never starts a service or calls a mutation.
for (const route of ['/', '/history', '/reports', '/ledger']) {
  test(`public dashboard ${route} renders current journal and fits viewport`, async ({ page }) => {
    const errors = [];
    page.on('pageerror', error => errors.push(error.message));
    const response = await page.goto(route);
    expect(response.status()).toBe(200);
    await expect(page.getByRole('navigation', { name: 'Main navigation' })).toBeVisible();
    if (route === '/history') {
      await expect(page.locator('#content')).toBeVisible();
      await expect(page.locator('#s-decisions')).toHaveText(/[\d,]+/);
      await expect(page.locator('#archive-status')).not.toHaveText('Loading archive…');
    } else if (route === '/reports') {
      await expect(page.locator('#content')).toBeVisible();
      await expect(page.locator('#stat-cycles')).toHaveText(/[\d,]+/);
    } else if (route === '/ledger') {
      await expect(page.locator('#api-v')).toHaveText(/Connected/i);
    } else {
      await expect(page.locator('#run-decisions')).toHaveText(/[\d,]+/);
      await expect(page.locator('#desk-updated')).not.toHaveText('Connecting to journal');
    }
    expect(errors).toEqual([]);
    expect(await page.evaluate(() => document.documentElement.scrollWidth - innerWidth)).toBeLessThanOrEqual(1);
  });
}

test('public API health and report counts agree with complete history', async ({ request }) => {
  const bodies = {};
  // End at yesterday in exchange time so active-session inserts cannot race
  // sequential read-only requests for decisions and distinct cycles.
  const today = new Intl.DateTimeFormat('en-CA', { timeZone: 'America/New_York' }).format(new Date());
  const prior = new Date(today + 'T12:00:00Z');
  prior.setUTCDate(prior.getUTCDate() - 1);
  const end = prior.toISOString().slice(0, 10);
  for (const route of ['/api/health', '/api/history', '/api/reports', '/api/end-of-day']) {
    const response = await request.get(route + (['/api/history', '/api/reports'].includes(route) ? '?end=' + end : ''));
    expect(response.status()).toBe(200);
    bodies[route] = await response.json();
  }
  expect(bodies['/api/history'].totals.decisions).toBe(bodies['/api/reports'].report.metrics.decisions);
  expect(bodies['/api/history'].totals.cycles).toBe(bodies['/api/reports'].report.metrics.cycles);
});
