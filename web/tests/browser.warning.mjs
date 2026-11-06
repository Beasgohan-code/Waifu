// Isolated browser test: synthetic API replies, no Telegram or database writes.
// Run only against the read-only sample frontend/API; POST is intercepted below.
import assert from 'node:assert/strict';
import { chromium } from 'playwright-core';

const url = process.env.SUMMON_PREVIEW_URL || 'http://127.0.0.1:5173';
const id = 'a'.repeat(32);
const browser = await chromium.launch({
  headless: true,
  ...(process.env.CHROMIUM_PATH ? { executablePath: process.env.CHROMIUM_PATH } : {}),
  args: ['--no-sandbox', '--disable-dev-shm-usage'],
});
const page = await browser.newPage({ viewport: { width: 320, height: 844 } });
// Preview tests do not depend on Telegram's remote bridge or its default theme.
await page.route('https://telegram.org/js/telegram-web-app.js', route => route.fulfill({ contentType: 'application/javascript', body: '' }));
await page.addInitScript(() => localStorage.setItem('summon-theme', 'dark'));
const errors = [];
let revoked = false;
let posts = 0;
page.on('pageerror', error => errors.push(error.message));

try {
  // Override only this browser's bootstrap; the server remains in read-only preview mode.
  await page.route('**/api/bootstrap', route => route.fulfill({ json: { data: { preview: false } } }));
  await page.route('**/api/groups/-100/members/456/warnings**', async route => {
    const request = route.request();
    if (request.method() === 'POST') {
      posts += 1;
      assert.equal(new URL(request.url()).pathname, `/api/groups/-100/members/456/warnings/${id}/revoke`);
      assert.equal(request.headers()['x-summon-request'], '1');
      assert.deepEqual(request.postDataJSON(), { reason: 'Recorded in error' });
      revoked = true;
      await route.fulfill({ json: { data: { id, status: 'revoked' } } });
    } else {
      await route.fulfill({ json: { data: revoked ? { items: [], active_count: 0, next_cursor: null } : {
        items: [{ id, actor_id: 1, reason: 'Synthetic warning', created_at: '2026-01-01T00:00:00Z' }],
        active_count: 1, next_cursor: null,
      } } });
    }
  });
  const groupsUrl = new URL(url);
  groupsUrl.hash = 'groups';
  await page.goto(groupsUrl.toString(), { waitUntil: 'networkidle' });
  await page.getByRole('tab', { name: 'Moderation' }).click();
  await page.getByLabel('Member ID to review').fill('456');
  await page.getByRole('button', { name: 'Find warnings' }).click();
  await page.getByText('Synthetic warning').waitFor();
  await page.getByRole('button', { name: 'Revoke warning' }).click();
  const confirmation = page.getByRole('region', { name: 'Confirm warning revocation' });
  await confirmation.waitFor();
  assert.ok(await confirmation.evaluate(element => document.activeElement === element), 'confirmation receives focus');
  assert.equal(posts, 0, 'opening the confirmation does not mutate');
  assert.ok(await confirmation.getByText(/member 456/).isVisible());
  assert.ok(await page.evaluate(() => document.body.scrollWidth <= window.innerWidth), 'confirmation fits 320px');
  assert.equal(await page.locator('html').getAttribute('data-theme'), 'dark');
  await page.getByRole('button', { name: 'Switch to light theme' }).click();
  assert.equal(await page.locator('html').getAttribute('data-theme'), 'light');
  assert.ok(await page.evaluate(() => document.body.scrollWidth <= window.innerWidth), 'light confirmation fits 320px');
  await confirmation.getByLabel('Reason for revocation (optional)').fill('Recorded in error');
  await confirmation.getByRole('button', { name: 'Confirm revocation' }).click();
  await page.getByText('No active warnings for this member.').waitFor();
  assert.equal(posts, 1, 'exactly one synthetic mutation was sent');
  assert.deepEqual(errors, []);
  console.log('Synthetic warning confirmation browser test passed at 320px; all API writes were intercepted.');
} finally {
  await page.close();
  await browser.close();
}
