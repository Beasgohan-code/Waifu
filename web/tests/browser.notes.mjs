// Isolated browser test against the sample preview: every note mutation is intercepted.
import assert from 'node:assert/strict';
import { chromium } from 'playwright-core';

const url = process.env.SUMMON_PREVIEW_URL || 'http://127.0.0.1:5173';
const revA = 'a'.repeat(32);
const revB = 'b'.repeat(32);
const base = { name: 'welcome', content: 'A synthetic group note.', revision: revA,
  created_at: '2026-01-01T00:00:00Z', updated_at: '2026-01-01T00:00:00Z', created_by: 1, updated_by: 1 };
let notes = [base];
let writes = 0;
const unexpectedWrites = [];
const errors = [];
const browser = await chromium.launch({
  headless: true,
  ...(process.env.CHROMIUM_PATH ? { executablePath: process.env.CHROMIUM_PATH } : {}),
  args: ['--no-sandbox', '--disable-dev-shm-usage'],
});
const page = await browser.newPage({ viewport: { width: 320, height: 844 } });
page.on('pageerror', error => errors.push(error.message));

try {
  // Refuse every unexpected write. The only writes accepted below are synthetic responses.
  await page.route('**/api/**', async route => {
    if (route.request().method() !== 'GET') {
      unexpectedWrites.push(route.request().method());
      await route.abort();
    } else await route.continue();
  });
  await page.route('**/api/bootstrap', route => route.fulfill({ json: { data: { preview: false } } }));
  await page.route('**/api/groups/-100/notes**', async route => {
    const request = route.request();
    const path = new URL(request.url()).pathname;
    if (request.method() === 'GET') return route.fulfill({ json: { data: notes } });
    assert.equal(request.headers()['x-summon-request'], '1');
    const body = request.postDataJSON();
    writes += 1;
    if (request.method() === 'PUT' && path.endsWith('/welcome')) {
      assert.deepEqual(body, { content: 'Updated synthetic note.', expected_revision: revA });
      notes = [{ ...base, content: body.content, revision: revB }];
      return route.fulfill({ json: { data: notes[0] } });
    }
    if (request.method() === 'POST' && path.endsWith('/welcome/delete')) {
      assert.deepEqual(body, { expected_revision: revB });
      notes = [];
      return route.fulfill({ json: { data: { name: 'welcome', status: 'deleted' } } });
    }
    if (request.method() === 'PUT' && path.endsWith('/guide')) {
      assert.deepEqual(body, { content: 'A new public note.', expected_revision: null });
      notes = [{ ...base, name: 'guide', content: body.content, revision: revA }];
      return route.fulfill({ json: { data: notes[0] } });
    }
    throw new Error(`Unexpected synthetic note request: ${request.method()} ${path}`);
  });

  const groupsUrl = new URL(url);
  groupsUrl.hash = 'groups';
  await page.goto(groupsUrl.toString(), { waitUntil: 'networkidle' });
  await page.getByRole('tab', { name: 'Notes' }).click();
  const text = page.getByLabel('Note text');
  await text.waitFor();
  assert.equal(await text.inputValue(), base.content);
  assert.ok(await page.getByText('Public in this group').isVisible());
  assert.ok(await page.evaluate(() => document.body.scrollWidth <= window.innerWidth), 'notes fit at 320px in dark theme');
  if (process.env.SUMMON_NOTES_SCREENSHOT) {
    await page.screenshot({ path: process.env.SUMMON_NOTES_SCREENSHOT, fullPage: true });
  }
  await page.getByRole('button', { name: 'Switch to light theme' }).click();
  assert.equal(await page.locator('html').getAttribute('data-theme'), 'light');
  assert.ok(await page.evaluate(() => document.body.scrollWidth <= window.innerWidth), 'notes fit at 320px in light theme');

  await text.fill('Updated synthetic note.');
  await page.getByRole('tab', { name: 'Overview' }).click();
  const draftConfirmation = page.getByRole('region', { name: 'Discard note draft' });
  await draftConfirmation.waitFor();
  assert.ok(await draftConfirmation.evaluate(element => document.activeElement === element), 'draft warning receives focus');
  assert.equal(writes, 0);
  await draftConfirmation.getByRole('button', { name: 'Keep editing' }).click();
  assert.equal(await text.inputValue(), 'Updated synthetic note.');
  await page.getByRole('button', { name: 'Save note' }).click();
  await page.waitForFunction(() => document.querySelector('textarea[aria-label="Note text"]')?.value === 'Updated synthetic note.');
  await page.getByRole('button', { name: 'Delete note' }).waitFor({ state: 'visible' });
  assert.equal(writes, 1);

  const opener = page.getByRole('button', { name: 'Delete note' });
  await opener.click();
  const confirmation = page.getByRole('region', { name: 'Confirm note deletion' });
  await confirmation.waitFor();
  assert.ok(await confirmation.evaluate(element => document.activeElement === element), 'delete confirmation receives focus');
  assert.ok(await confirmation.getByText(/cannot be undone/i).isVisible());
  assert.equal(writes, 1);
  await confirmation.getByRole('button', { name: 'Cancel' }).click();
  assert.ok(await opener.evaluate(element => document.activeElement === element), 'cancel restores focus');
  await opener.click();
  await page.getByRole('button', { name: 'Confirm deletion' }).click();
  await page.getByLabel('Note name').waitFor();
  assert.equal(writes, 2);

  await page.getByLabel('Note name').fill('guide');
  await page.getByLabel('Note text').fill('A new public note.');
  await page.getByRole('button', { name: 'Save note' }).click();
  await page.getByRole('button', { name: 'Edit note guide' }).waitFor();
  assert.equal(writes, 3);
  assert.deepEqual(unexpectedWrites, []);
  assert.deepEqual(errors, []);
  console.log('Synthetic group notes browser test passed at 320px in dark/light; every API write was intercepted.');
} finally {
  await page.close();
  await browser.close();
}
