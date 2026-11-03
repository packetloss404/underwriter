'use strict';
const { test, expect } = require('@playwright/test');
const fs = require('node:fs/promises');

// Every normal response comes from the real FastAPI routes and SQLite journal.
// Request interception is restricted to deliberate outage/empty-state scenarios.
test.beforeEach(async ({ page }) => {
  const errors = [];
  page.on('pageerror', error => errors.push(error.message));
  page.__errors = errors;
  // Fonts are cosmetic and must not make this fixture depend on external DNS.
  await page.route(/https:\/\/fonts\.(googleapis|gstatic)\.com\//, route => route.abort());
});
test.afterEach(async ({ page }) => {
  expect(page.__errors).toEqual([]);
});

async function history(page, suffix = '') {
  await page.goto('/history' + suffix);
  await expect(page.locator('#content')).toBeVisible();
  await expect(page.locator('#archive-list .event').first()).toBeVisible();
}
async function downloadText(page, button) {
  const pending = page.waitForEvent('download');
  await page.locator(button).click();
  const download = await pending;
  return { name: download.suggestedFilename(), text: await fs.readFile(await download.path(), 'utf8') };
}
async function emptyApi(page) {
  await page.route('**/api/**', async route => {
    const url = new URL(route.request().url());
    url.pathname = '/empty' + url.pathname;
    await route.continue({ url: url.toString() });
  });
}

test('overview renders account, book, full history, charts and shared navigation', async ({ page }) => {
  await page.goto('/');
  await expect(page.locator('#desk-equity')).toHaveText('$100,240.00');
  await expect(page.locator('#run-decisions')).toHaveText('129');
  await expect(page.locator('#run-fills')).toHaveText('2');
  await expect(page.locator('#book-panel')).toContainText('XLE');
  await expect(page.locator('#desk-equity-chart svg')).toBeVisible();
  await expect(page.locator('#run-activity svg')).toBeVisible();
  const nav = page.getByRole('navigation', { name: 'Main navigation' });
  await nav.getByRole('link', { name: 'History', exact: true }).click();
  await expect(page).toHaveURL(/\/history$/);
  await expect(page.locator('#s-decisions')).toHaveText('129');
  await page.getByRole('navigation', { name: 'Main navigation' }).getByRole('link', { name: 'Reports', exact: true }).click();
  await expect(page.locator('#stat-fills')).toHaveText('1');
  await page.getByRole('navigation', { name: 'Main navigation' }).getByRole('link', { name: 'Ledger', exact: true }).click();
  await expect(page.locator('#orders-panel')).toContainText('XLE');
});

test('history totals, stable older/newer pagination, expanded context and cycle search', async ({ page }) => {
  await history(page);
  await expect(page.locator('#s-decisions')).toHaveText('129');
  await expect(page.locator('#archive-list .event')).toHaveCount(50);
  const first = await page.locator('#archive-list .event').first().getAttribute('data-record');
  await page.locator('#next').click();
  await expect(page.locator('#page-info')).toContainText('2');
  await expect(page.locator('#archive-list .event')).toHaveCount(50);
  expect(await page.locator('#archive-list .event').first().getAttribute('data-record')).not.toBe(first);
  await page.locator('#prev').click();
  await expect(page.locator('#archive-list .event').first()).toHaveAttribute('data-record', first);
  await page.locator('#archive-list .event').first().locator('summary').click();
  await expect(page.locator('#archive-list .event').first().locator('pre')).toContainText('vrp_ratio');
  await page.locator('#archive-list .event').first().getByRole('button', { name: 'Explore this cycle' }).click();
  await expect(page.locator('#archive-list .event')).toHaveCount(1);
  await expect(page).toHaveURL(/q=browser-cycle/);
  await page.reload();
  await expect(page.locator('#archive-list .event')).toHaveCount(1);
  await page.locator('#clear-filters').click();
  await expect(page.locator('#archive-list .event')).toHaveCount(50);
});

test('filters distinguish operational, strategy and exploratory records with reloadable URLs', async ({ page }) => {
  await history(page, '?track=live&outcome=operational#archive');
  await expect(page.locator('#archive-list .event')).toHaveCount(1);
  await expect(page.locator('#archive-list')).toContainText('Service failure');
  await expect(page.locator('#f-track')).toHaveValue('live');
  await page.locator('#archive-list summary').click();
  await expect(page.locator('#archive-list')).toContainText('Model unavailable');
  await page.locator('#f-outcome').selectOption('strategy');
  await page.locator('#f-symbol').selectOption('SPY');
  await page.locator('#archive-form').getByRole('button', { name: 'Search', exact: true }).click();
  await expect(page.locator('#archive-list')).not.toContainText('Service failure');
  await expect(page.locator('#archive-list .event').first()).toContainText('SPY');
  await page.locator('#clear-filters').click();
  await page.locator('#f-track').selectOption('exploratory');
  await page.locator('#archive-form').getByRole('button', { name: 'Search', exact: true }).click();
  await expect(page.locator('#archive-list .event')).toHaveCount(1);
  await expect(page.locator('#archive-list')).toContainText('Exploratory');
  await page.reload();
  await expect(page.locator('#f-track')).toHaveValue('exploratory');
  await expect(page.locator('#archive-list .event')).toHaveCount(1);
});

test('date presets/custom dates, chart readouts, independent sources and CSV totals', async ({ page }) => {
  await history(page);
  await page.locator('[data-range="7"]').click();
  await expect(page.locator('#start')).toHaveValue('2026-08-22');
  await page.locator('#start').fill('2026-08-26');
  await page.locator('#end').fill('2026-08-26');
  await page.locator('#range-form').getByRole('button', { name: 'Apply', exact: true }).click();
  await expect(page.locator('#s-decisions')).toHaveText('41');
  await page.locator('[data-metric="cycles"]').click();
  await expect(page.locator('#activity-total')).toContainText('41 cycles');
  await page.locator('#activity-chart .hit').first().focus();
  await expect(page.locator('#activity-readout')).toContainText('41');
  await page.locator('[data-range="all"]').click();
  await expect(page.locator('#s-decisions')).toHaveText('129');
  await page.locator('#equity-source').selectOption('shadow');
  await expect(page.locator('#equity-latest')).toContainText('$100,223.00');
  await page.locator('#equity-source').selectOption('exploratory');
  await expect(page.locator('#equity-chart')).toContainText('Missing measurements are not zero');
  await page.locator('#equity-source').selectOption('official');
  await expect(page.locator('#equity-latest')).toContainText('$100,240.00');
  const csv = await downloadText(page, '#download');
  expect(csv.name).toBe('underwriter-2026-08-26-2026-08-28-day.csv');
  const rows = csv.text.trim().split(/\r?\n/).map(row => row.split(','));
  expect(rows[0]).toContain('official_equity');
  expect(rows.slice(1).reduce((sum, row) => sum + Number(row[2]), 0)).toBe(129);
  await page.locator('#heatmap button').first().click();
  await expect(page.locator('#start')).toHaveValue('2026-08-26');
  await expect(page.locator('#end')).toHaveValue('2026-08-26');
});

test('symbol/reason/stage drilldowns and search expose the corresponding real records', async ({ page }) => {
  await history(page);
  await page.locator('#symbols-body [data-symbol="SPY"]').click();
  await expect(page.locator('#f-symbol')).toHaveValue('SPY');
  await expect(page.locator('#f-track')).toHaveValue('live');
  await page.locator('#reasons [data-reason="model_unavailable"]').click();
  await expect(page.locator('#archive-list .event')).toHaveCount(1);
  await page.locator('#stages [data-stage="rank"]').click();
  await expect(page.locator('#f-stage')).toHaveValue('rank');
  await page.locator('#f-query').fill('browser-cycle-001');
  await page.locator('#archive-form').getByRole('button', { name: 'Search', exact: true }).click();
  await expect(page.locator('#archive-list .event')).toHaveCount(1);
  await expect(page.locator('#archive-list')).toContainText('QQQ');
});

test('reports display execution/control evidence, provenance, comparison, CSV and filtered archive links', async ({ page }) => {
  await page.goto('/reports');
  await expect(page.locator('#content')).toBeVisible();
  await expect(page.locator('#stat-fills')).toHaveText('1');
  await expect(page.locator('#stat-affected')).toHaveText('1');
  await expect(page.locator('#execution-body')).toContainText('Filled');
  await expect(page.locator('#execution-body')).toContainText('2 orders with recorded fills');
  await expect(page.locator('#execution-body')).toContainText('5 cumulative filled spread units');
  await expect(page.locator('#execution-body')).toContainText('Execution event coverage is incomplete');
  await expect(page.locator('[data-source="shadow"]')).toContainText(/does not independently validate/);
  await expect(page.locator('#controls')).toContainText('Full');
  await expect(page.locator('#comparison-rows')).toContainText('Not observed');
  await expect(page.locator('[data-source="official"] .source-equity')).toHaveText('$100,240.00');
  await expect(page.locator('[data-source="shadow"] .source-equity')).toHaveText('$100,223.00');
  await expect(page.locator('[data-source="exploratory"] .source-equity')).toHaveText('Unknown');
  const csv = await downloadText(page, '#export');
  expect(csv.name).toBe('underwriter-report-2026-08-26-2026-08-28.csv');
  expect(csv.text).toContain('"Parent fill records","1"');
  await page.evaluate(() => { window.__prints = 0; window.print = () => window.__prints++; });
  await page.locator('#print').click();
  expect(await page.evaluate(() => window.__prints)).toBe(1);
  await page.emulateMedia({ media: 'print' });
  await expect(page.locator('#range-form')).toBeHidden();
  await expect(page.locator('#content')).toBeVisible();
  await page.emulateMedia({ media: 'screen' });
  await page.locator('[data-range="7"]').click();
  await expect(page.locator('#start')).toHaveValue('2026-08-22');
  await page.locator('#start').fill('2026-08-28');
  await page.locator('#end').fill('2026-08-28');
  await page.locator('#range-form').getByRole('button', { name: 'Apply', exact: true }).click();
  await expect(page.locator('#prior-heading')).toContainText('1 observed days');
  await page.locator('[data-range="all"]').click();
  await expect(page.locator('#receipt-range')).toHaveText('2026-08-26 — 2026-08-28');
  await page.locator('#link-failures').click();
  await expect(page.locator('#archive-list .event')).toHaveCount(1);
  await expect(page.locator('#f-outcome')).toHaveValue('operational');
  await expect(page.locator('#f-track')).toHaveValue('live');
});

test('ledger search, recorded context and orders use current API envelopes', async ({ page }) => {
  await page.goto('/ledger');
  await expect(page.locator('#api-v')).toHaveText(/Connected/i);
  await expect(page.locator('#orders-panel')).toContainText('XLE');
  await expect(page.locator('#positions-panel')).toContainText('XLE');
  await page.locator('#ledger-query').fill('browser-cycle-001');
  await expect(page.locator('#decisions-panel tbody tr')).toHaveCount(1);
  await page.locator('#decisions-panel summary').click();
  await expect(page.locator('#decisions-panel pre')).toContainText('vrp_ratio');
  await page.locator('#decisions-panel').getByRole('link', { name: 'Explore this cycle' }).click();
  await expect(page.locator('#archive-list .event')).toHaveCount(1);
  await expect(page.locator('#archive-list')).toContainText('QQQ');
});

for (const route of ['/', '/ledger']) {
  test(`clearly labelled specimen mode at ${route}`, async ({ page }) => {
    await page.goto(route + '?demo');
    await expect(page.locator('#demo-banner')).toBeVisible();
    await expect(page.locator('body')).toHaveClass(/demo/);
    await expect(page.locator('#demo-banner')).toContainText(/specimen|demo/i);
    expect(await page.locator('body').innerText()).not.toContain('browser-cycle-');
  });
}

for (const route of ['/', '/history', '/reports', '/ledger']) {
  test(`empty journal is explicit and responsive at ${route}`, async ({ page }) => {
    await emptyApi(page);
    await page.goto(route);
    if (route === '/history') {
      await expect(page.locator('#content')).toBeVisible();
      await expect(page.locator('#s-decisions')).toHaveText('0');
      await expect(page.locator('#coverage')).toContainText('No journal observations');
    } else if (route === '/reports') {
      await expect(page.locator('#content')).toBeVisible();
      await expect(page.locator('#stat-cycles')).toHaveText('0');
      await expect(page.locator('#execution-body')).toContainText('No order or fill records');
    } else if (route === '/ledger') {
      await expect(page.locator('#decisions-panel')).toContainText('No decisions are recorded');
    } else {
      await expect(page.locator('#desk-equity')).toHaveText('Unknown');
      await expect(page.locator('#run-decisions')).toHaveText('0');
    }
    const overflow = await page.evaluate(() => document.documentElement.scrollWidth - window.innerWidth);
    expect(overflow).toBeLessThanOrEqual(1);
  });
}

for (const route of ['/history', '/reports']) {
  test(`failed aggregate requests show retryable errors at ${route}`, async ({ page }) => {
    const endpoint = route === '/history' ? '/api/history' : '/api/reports';
    await page.route('**' + endpoint + '?*', r => r.fulfill({ status: 503, json: { detail: 'Audit fixture unavailable' } }));
    await page.goto(route);
    await expect(page.locator('#status')).toContainText('Audit fixture unavailable');
    await expect(page.locator('#status')).toHaveClass(/error/);
    await page.unroute('**' + endpoint + '?*');
    await page.locator('#refresh').click();
    await expect(page.locator('#content')).toBeVisible();
    await expect(page.locator('#status')).not.toHaveClass(/error/);
  });
}

test('static export is a self-contained labelled snapshot with working evidence and ledger navigation', async ({ page }) => {
  const failed = [];
  page.on('response', response => { if (response.status() >= 400) failed.push(response.url()); });
  page.on('requestfailed', request => failed.push(request.url()));
  await page.goto('/snapshot/');
  await expect(page.getByText('Static snapshot · not live', { exact: false })).toBeVisible();
  await expect(page.getByRole('heading', { level: 1 })).toContainText('Snapshot');
  await expect(page.locator('body')).toContainText('XLE');
  await page.getByRole('link', { name: /ledger/i }).first().click();
  await expect(page).toHaveURL(/\/snapshot\/ledger.html$/);
  await expect(page.locator('body')).toContainText('Browser audit observation');
  await expect(page.locator('script')).toHaveCount(0);
  expect(failed).toEqual([]);
});

test('all populated pages fit a narrow 320 pixel screen', async ({ page }) => {
  await page.setViewportSize({ width: 320, height: 740 });
  for (const route of ['/', '/history', '/reports', '/ledger']) {
    await page.goto(route);
    if (route === '/history' || route === '/reports') await expect(page.locator('#content')).toBeVisible();
    else if (route === '/') await expect(page.locator('#run-decisions')).toHaveText('129');
    else await expect(page.locator('#orders-panel')).toContainText('XLE');
    expect(await page.evaluate(() => document.documentElement.scrollWidth - innerWidth), route).toBeLessThanOrEqual(1);
  }
});



test('guided replay keeps live evidence and specimen chapters distinct and returns focus', async ({ page }) => {
  await page.goto('/');
  await expect(page.locator('#api-v')).toHaveText('Connected');
  await page.locator('#replay-launch').click();
  await expect(page.locator('#replay-shell')).toBeVisible();
  await expect(page.locator('#replay-mode')).toHaveText('Live evidence');
  await expect(page.locator('#replay-close')).toBeFocused();
  await page.locator('#replay-toggle').click();
  await expect(page.locator('#replay-toggle')).toHaveText('Play');
  const title = await page.locator('#replay-title').innerText();
  await page.locator('#replay-next').click();
  await expect(page.locator('#replay-title')).not.toHaveText(title);
  await page.locator('#replay-prev').click();
  await expect(page.locator('#replay-title')).toHaveText(title);
  await page.locator('#replay-speed').click();
  await expect(page.locator('#replay-speed')).toHaveText('1.5× speed');
  await page.locator('#replay-speed').click();
  await expect(page.locator('#replay-speed')).toHaveText('2× speed');
  const chapters = page.locator('#replay-progress button');
  await chapters.nth(6).click();
  await expect(page.locator('#replay-mode')).toHaveText('Specimen walkthrough');
  await expect(page.locator('.replay-disclosure')).toHaveText('Specimen walkthrough · not an observed result');
  await chapters.nth(7).click();
  await expect(page.locator('#replay-mode')).toHaveText('Specimen walkthrough');
  await expect(page.locator('.replay-disclosure')).toHaveText('Specimen walkthrough · no broker order was sent');
  await chapters.last().click();
  await expect(page.locator('#replay-mode')).toHaveText('Live evidence');
  await page.keyboard.press('Escape');
  await expect(page.locator('#replay-shell')).toBeHidden();
  await expect(page.locator('#replay-launch')).toBeFocused();
});

for (const route of ['/', '/ledger']) {
  test(`unavailable journal is explicit without invented account values at ${route}`, async ({ page }) => {
    await page.route('**/api/**', r => r.fulfill({ status: 503, json: { detail: 'Audit journal unavailable' } }));
    await page.goto(route);
    await expect(page.locator('#api-v')).toHaveText('Offline');
    if (route === '/') await expect(page.locator('#desk-equity')).toHaveText('Unknown');
    else await expect(page.locator('#decisions-panel')).toContainText('Unavailable');
    await page.unroute('**/api/**');
    if (route === '/') await page.locator('#desk-refresh').click();
    else await page.reload();
    await expect(page.locator('#api-v')).toHaveText('Connected');
  });
}

for (const route of ['/history', '/reports']) {
  test(`new closeout receipt refreshes real aggregates and preserves selected scope at ${route}`, async ({ page }) => {
    await page.clock.install();
    let completed = false, reads = 0;
    page.on('request', request => {
      if (new URL(request.url()).pathname === (route === '/history' ? '/api/history' : '/api/reports')) reads++;
    });
    await page.route('**/api/end-of-day', r => r.fulfill({ json: completed ? {
      enabled: true, status: 'complete', session_date: '2026-08-28', completed_session_date: '2026-08-28',
      completed_at: '2026-08-28T20:20:05Z', observed_at: '2026-08-28T20:20:00Z',
    } : { enabled: false, status: 'disabled' } }));
    await page.goto(route + '?start=2026-08-26&end=2026-08-28' + (route === '/history' ? '&symbol=SPY&source=shadow#archive' : ''));
    await expect(page.locator('#content')).toBeVisible();
    let pinnedRecord = null;
    if (route === '/history') {
      await expect(page.locator('#archive-list .event').first()).toBeVisible();
      await page.locator('#next').click();
      await expect(page.locator('#page-info')).toHaveText('Records 51–63 of 63');
      pinnedRecord = await page.locator('#archive-list .event').first().getAttribute('data-record');
    }
    await expect(page.locator('#eod-status')).toHaveAttribute('data-state', 'disabled');
    await page.locator('#refresh').focus();
    const prior = reads;
    completed = true;
    await page.clock.fastForward(60000);
    await expect(page.locator('#eod-status')).toContainText('End-of-day completed for 2026-08-28');
    await expect.poll(() => reads).toBeGreaterThan(prior);
    await expect(page.locator('#status')).toContainText(route === '/history' ? 'Complete selected history' : 'All selected records counted');
    await expect(page.locator('#start')).toHaveValue('2026-08-26');
    await expect(page.locator('#end')).toHaveValue('2026-08-28');
    if (route === '/history') {
      await expect(page.locator('#f-symbol')).toHaveValue('SPY');
      await expect(page.locator('#equity-source')).toHaveValue('shadow');
      await expect(page.locator('#page-info')).toHaveText('Records 51–63 of 63');
      await expect(page.locator('#archive-list .event').first()).toHaveAttribute('data-record', pinnedRecord);
    }
  });
}

async function executionApi(page, mode) {
  await page.route('**/api/**', async route => {
    const url = new URL(route.request().url());
    url.pathname = '/execution-' + mode + url.pathname;
    await route.continue({ url: url.toString() });
  });
}

test('production ingestion preserves partial activity IDs, derives one spread group and survives duplicates/restart', async ({ page, request }) => {
  // Server setup uses the production ActivityReader and ExecutionIngestor, repeats
  // delivery, reopens SQLite, and repeats it again before serving these HTTP reads.
  const response = await request.get('/execution-complete/api/orders');
  expect(response.status()).toBe(200);
  const body = await response.json();
  expect(body.execution_ingestion.status).toBe('complete');
  expect(body.orders).toHaveLength(1);
  expect(body.orders[0].fills).toHaveLength(1);
  const group = body.orders[0].fills[0];
  expect(group.provenance.kind).toBe('alpaca_leg_activity_aggregate_v1');
  expect(group.provenance.timestamp_semantics).toBe('latest_component_execution_time');
  expect(new Set(group.provenance.activity_ids)).toEqual(new Set([
    'browser-raw-short-partial', 'browser-raw-short-final', 'browser-raw-long-final',
  ]));
  expect(group.spreads).toBe(3);
  expect(group.net_price_per_spread_signed).toBeCloseTo(-1.2);
  expect(group.legs).toHaveLength(3);
  expect(group.legs.reduce((sum, row) => sum + row.contracts, 0)).toBe(6);
  expect(group.legs.find(row => row.fill_id === 'browser-raw-short-partial').occurred_at).toBe('2026-08-28T18:26:00.123456+00:00');
  expect(group.occurred_at).toBe('2026-08-28T18:27:01.345678+00:00');
  await executionApi(page, 'complete');
  await page.goto('/reports');
  await expect(page.locator('#execution-ingestion')).toHaveAttribute('data-state', 'complete');
  await expect(page.locator('#execution-coverage')).toHaveAttribute('data-state', 'complete');
  for (const [key, count] of Object.entries({ 'raw-leg-activities': 3, 'derived-groups': 1, 'parent-records': 1, 'spread-units': 3, 'leg-records': 3, 'leg-contracts': 6 })) {
    await expect(page.locator('[data-evidence="' + key + '"]')).toHaveText(String(count));
  }
  await expect(page.locator('#execution-body')).toContainText('not broker-issued parent execution events');
  await page.getByRole('navigation', { name: 'Main navigation' }).getByRole('link', { name: 'Ledger', exact: true }).click();
  await page.locator('.order-evidence summary').click();
  await expect(page.locator('.order-evidence')).toContainText('Derived spread group');
  await expect(page.locator('.order-evidence')).toContainText('latest component execution');
  await expect(page.locator('.order-evidence')).toContainText('browser-raw-short-partial');
  await expect(page.locator('.order-evidence')).toContainText('2026-08-28T18:26:00.123456+00:00');
  await expect(page.locator('.order-evidence')).toContainText('1 contracts');
});

test('unbalanced raw broker activity remains pending and visible without an invented parent fill', async ({ page, request }) => {
  const body = await (await request.get('/execution-pending/api/orders')).json();
  expect(body.execution_ingestion.status).toBe('pending');
  expect(body.orders[0].fills).toHaveLength(0);
  expect(body.orders[0].unlinked_leg_events).toHaveLength(1);
  expect(body.orders[0].unlinked_leg_events[0].fill_id).toBe('browser-raw-short-partial');
  await executionApi(page, 'pending');
  await page.goto('/reports');
  await expect(page.locator('#execution-ingestion')).toHaveAttribute('data-state', 'pending');
  await expect(page.locator('#execution-coverage')).toHaveAttribute('data-state', 'pending');
  await expect(page.locator('[data-evidence="raw-leg-activities"]')).toHaveText('1');
  await expect(page.locator('[data-evidence="derived-groups"]')).toHaveText('0');
  await expect(page.locator('[data-evidence="parent-records"]')).toHaveText('0');
  await page.getByRole('navigation', { name: 'Main navigation' }).getByRole('link', { name: 'Ledger', exact: true }).click();
  await page.locator('.order-evidence summary').click();
  await expect(page.locator('.order-evidence')).toContainText('Pending spread grouping');
  await expect(page.locator('.order-evidence')).toContainText('browser-raw-short-partial');
  await expect(page.locator('.order-evidence')).not.toContainText('Derived spread group');
});

test('conflicting broker leg identity produces a durable conflict receipt and no fabricated derived group', async ({ page, request }) => {
  const body = await (await request.get('/execution-conflict/api/orders')).json();
  expect(body.execution_ingestion.status).toBe('conflict');
  expect(body.execution_ingestion.issues.length).toBeGreaterThan(0);
  expect(body.orders[0].fills).toHaveLength(0);
  await executionApi(page, 'conflict');
  await page.goto('/reports');
  await expect(page.locator('#execution-ingestion')).toHaveAttribute('data-state', 'conflict');
  await expect(page.locator('#execution-ingestion')).toContainText('Latest broker activity sync · Conflict');
  await expect(page.locator('#execution-coverage')).toHaveAttribute('data-state', 'conflict');
  await expect(page.locator('[data-evidence="derived-groups"]')).toHaveText('0');
  await expect(page.locator('[data-evidence="parent-records"]')).toHaveText('0');
});
