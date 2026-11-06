// Synthetic browser test: every auto-reply mutation is intercepted, never sent to MongoDB.
import assert from 'node:assert/strict';
import { chromium } from 'playwright-core';

const url = process.env.SUMMON_PREVIEW_URL || 'http://127.0.0.1:5173';
const revA = 'a'.repeat(32);
const revB = 'b'.repeat(32);
const base = { name: 'greeting', trigger: 'hello everyone', response: 'A synthetic public reply.', enabled: false,
  revision: revA, created_at: '2026-01-01T00:00:00Z', updated_at: '2026-01-01T00:00:00Z', created_by: 1, updated_by: 1 };
let rules = [base];
let writes = 0;
const unexpectedWrites = [];
const errors = [];
const browser = await chromium.launch({
  headless: true,
  ...(process.env.CHROMIUM_PATH ? { executablePath: process.env.CHROMIUM_PATH } : {}),
  args: ['--no-sandbox', '--disable-dev-shm-usage'],
});
const page = await browser.newPage({ viewport: { width: 320, height: 844 } });
await page.route('https://telegram.org/js/telegram-web-app.js', route => route.fulfill({ contentType: 'application/javascript', body: '' }));
await page.addInitScript(() => localStorage.setItem('summon-theme', 'dark'));
page.on('pageerror', error => errors.push(error.message));

try {
  await page.route('**/api/**', async route => {
    if (route.request().method() !== 'GET') { unexpectedWrites.push(route.request().method()); await route.abort(); }
    else await route.continue();
  });
  await page.route('**/api/bootstrap', route => route.fulfill({ json: { data: { preview: false } } }));
  await page.route('**/api/groups/-100/auto-replies**', async route => {
    const request = route.request();
    const path = new URL(request.url()).pathname;
    if (request.method() === 'GET') return route.fulfill({ json: { data: rules } });
    assert.equal(request.headers()['x-summon-request'], '1');
    const body = request.postDataJSON();
    writes += 1;
    if (request.method() === 'PUT' && path.endsWith('/greeting')) {
      assert.deepEqual(body, { trigger: 'hello everyone', response: 'Updated synthetic reply.', enabled: true, expected_revision: revA });
      rules = [{ ...base, response: body.response, enabled: true, revision: revB }];
      return route.fulfill({ json: { data: rules[0] } });
    }
    if (request.method() === 'POST' && path.endsWith('/greeting/delete')) {
      assert.deepEqual(body, { expected_revision: revB });
      rules = [];
      return route.fulfill({ json: { data: { name: 'greeting', status: 'deleted' } } });
    }
    if (request.method() === 'PUT' && path.endsWith('/guide') && body.expected_revision === null) {
      assert.deepEqual(body, { trigger: 'read the guide', response: 'A new public reply.', enabled: false, expected_revision: null });
      rules = [{ ...base, name: 'guide', trigger: body.trigger, response: body.response, enabled: false }];
      return route.fulfill({ json: { data: rules[0] } });
    }
    if (request.method() === 'PUT' && path.endsWith('/guide') && body.expected_revision === revA) {
      return route.fulfill({ status: 409, json: { error: { message: 'Synthetic conflict' } } });
    }
    throw new Error(`Unexpected synthetic auto-reply request: ${request.method()} ${path}`);
  });

  const groupsUrl = new URL(url);
  groupsUrl.hash = 'groups';
  await page.goto(groupsUrl.toString(), { waitUntil: 'networkidle' });
  await page.getByRole('tab', { name: 'Auto replies' }).click();
  const text = page.getByLabel('Reply text');
  await text.waitFor();
  assert.equal(await text.inputValue(), base.response);
  assert.ok(await page.getByText('Public in this group').isVisible());
  assert.ok(!(await page.getByLabel('Enable this auto reply').isChecked()));
  assert.equal(await page.locator('html').getAttribute('data-theme'), 'dark');
  assert.ok(await page.evaluate(() => document.body.scrollWidth <= window.innerWidth), 'auto replies fit at 320px in dark theme');
  if (process.env.SUMMON_AUTO_REPLIES_SCREENSHOT) {
    await page.screenshot({ path: process.env.SUMMON_AUTO_REPLIES_SCREENSHOT, fullPage: true });
  }
  await page.getByRole('button', { name: 'Switch to light theme' }).click();
  assert.equal(await page.locator('html').getAttribute('data-theme'), 'light');
  assert.ok(await page.evaluate(() => document.body.scrollWidth <= window.innerWidth), 'auto replies fit at 320px in light theme');

  await text.fill('Updated synthetic reply.');
  await page.getByLabel('Enable this auto reply').check();
  await page.getByRole('tab', { name: 'Overview' }).click();
  const draftConfirmation = page.getByRole('region', { name: 'Discard auto-reply draft' });
  await draftConfirmation.waitFor();
  assert.ok(await draftConfirmation.evaluate(element => document.activeElement === element), 'draft warning receives focus');
  assert.equal(writes, 0);
  await draftConfirmation.getByRole('button', { name: 'Keep editing' }).click();
  assert.equal(await text.inputValue(), 'Updated synthetic reply.');
  await page.getByRole('button', { name: 'Save auto reply' }).click();
  await page.waitForFunction(() => document.querySelector('textarea[aria-label="Reply text"]')?.value === 'Updated synthetic reply.');
  await page.getByRole('button', { name: 'Delete auto reply' }).waitFor({ state: 'visible' });
  assert.equal(writes, 1);

  const opener = page.getByRole('button', { name: 'Delete auto reply' });
  await opener.click();
  const confirmation = page.getByRole('region', { name: 'Confirm auto-reply deletion' });
  await confirmation.waitFor();
  assert.ok(await confirmation.evaluate(element => document.activeElement === element), 'delete confirmation receives focus');
  assert.ok(await confirmation.getByText(/cannot be undone/i).isVisible());
  await confirmation.getByRole('button', { name: 'Cancel' }).click();
  assert.ok(await opener.evaluate(element => document.activeElement === element), 'cancel restores focus');
  await opener.click();
  await page.getByRole('button', { name: 'Confirm deletion' }).click();
  await page.getByLabel('Rule name').waitFor();
  assert.equal(writes, 2);

  await page.getByLabel('Rule name').fill('guide');
  await page.getByLabel('Exact trigger').fill('read the guide');
  await page.getByLabel('Reply text').fill('A new public reply.');
  assert.ok(!(await page.getByLabel('Enable this auto reply').isChecked()));
  await page.getByRole('button', { name: 'Save auto reply' }).click();
  await page.getByRole('button', { name: 'Edit auto reply guide' }).waitFor();
  assert.equal(writes, 3);
  await page.getByLabel('Reply text').fill('Keep this conflicting draft.');
  await page.getByRole('button', { name: 'Save auto reply' }).click();
  await page.getByRole('alert').getByText(/copy your draft/i).waitFor();
  assert.equal(await page.getByLabel('Reply text').inputValue(), 'Keep this conflicting draft.');
  assert.ok(await page.getByRole('button', { name: 'Save auto reply' }).isDisabled());
  await page.getByRole('button', { name: /Reload auto replies.*discard draft/i }).click();
  // Wait for the intercepted list response, not merely for the reload click.
  await page.waitForFunction(() => document.querySelector('textarea[aria-label="Reply text"]')?.value === 'A new public reply.');
  assert.equal(await page.getByLabel('Reply text').inputValue(), 'A new public reply.');
  assert.equal(writes, 4);
  assert.deepEqual(unexpectedWrites, []);
  assert.deepEqual(errors, []);
  console.log('Synthetic auto-reply browser test passed at 320px in dark/light; every API write was intercepted.');
} finally {
  await page.close();
  await browser.close();
}
