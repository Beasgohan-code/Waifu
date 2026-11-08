// Browser session-isolation smoke test against tests/browser_auth_fixture.py ONLY.
// Uses synthetic Telegram signatures and an in-memory store, not a live bot or MongoDB.
import assert from 'node:assert/strict';
import { createHmac } from 'node:crypto';
import { chromium } from 'playwright-core';

const url = process.env.SUMMON_PREVIEW_URL || 'http://127.0.0.1:5173';
const executablePath = process.env.CHROMIUM_PATH;
const token = '123456:TEST_TOKEN_DO_NOT_USE';

function launch(userId, name) {
  const fields = {
    auth_date: String(Math.floor(Date.now() / 1000)),
    user: JSON.stringify({ id: userId, first_name: name }),
  };
  const text = Object.entries(fields).sort(([a], [b]) => a.localeCompare(b)).map(([k, v]) => `${k}=${v}`).join('\n');
  const secret = createHmac('sha256', 'WebAppData').update(token).digest();
  fields.hash = createHmac('sha256', secret).update(text).digest('hex');
  return new URLSearchParams(fields).toString();
}

const browser = await chromium.launch({
  headless: true,
  ...(executablePath ? { executablePath } : {}),
  args: ['--no-sandbox', '--disable-dev-shm-usage'],
});
const context = await browser.newContext();
// The local fixture replaces Telegram's script; this is NOT a live Telegram test.
await context.route('https://telegram.org/js/telegram-web-app.js', route => route.fulfill({ body: '', contentType: 'text/javascript' }));

async function newTelegramTab(initData) {
  const page = await context.newPage();
  await page.addInitScript(data => {
    window.Telegram = { WebApp: { initData: data, ready() {}, expand() {}, onEvent() {}, offEvent() {} } };
  }, initData);
  await page.goto(url, { waitUntil: 'networkidle' });
  return page;
}

async function apiStatus(page, path, method = 'get') {
  return page.evaluate(async ({ path: requestPath, method: requestMethod }) => {
    const { api } = await import('/src/api.ts');
    try { await api[requestMethod](requestPath); return 200; }
    catch (error) { return error.status || 0; }
  }, { path, method });
}

try {
  const alice = await newTelegramTab(launch(123, 'Alice'));
  await alice.getByRole('heading', { name: 'Welcome back, Alice.' }).waitFor();
  const [sessionCookie] = (await context.cookies()).filter(cookie => cookie.name === 'summon_session');
  assert.equal(sessionCookie.httpOnly, true);
  assert.equal(sessionCookie.sameSite, 'Strict');

  // Both tabs deliberately share the same browser cookie jar.
  const bob = await newTelegramTab(launch(456, 'Bob'));
  await bob.getByRole('heading', { name: 'Welcome back, Bob.' }).waitFor();
  const [bobCookie] = (await context.cookies()).filter(cookie => cookie.name === 'summon_session');
  assert.equal(await apiStatus(alice, '/me'), 401);
  assert.equal(await apiStatus(alice, '/inventory'), 401);
  assert.equal(await apiStatus(alice, '/auth/logout', 'post'), 401);
  assert.equal(await apiStatus(bob, '/me'), 200);
  assert.equal(await bob.evaluate(async () => (await fetch('/api/me')).status), 401);

  let signIns = 0;
  bob.on('request', request => { if (request.method() === 'POST' && request.url().endsWith('/api/auth/session')) signIns += 1; });
  await bob.reload({ waitUntil: 'networkidle' });
  await bob.getByRole('heading', { name: 'Welcome back, Bob.' }).waitFor();
  assert.equal(signIns, 0, 'same launch resumes without replaying the one-time proof');

  await alice.reload({ waitUntil: 'networkidle' });
  await alice.getByText('Telegram launch data has already been used; reopen SUMMON').waitFor();
  assert.equal(await alice.getByRole('heading', { name: 'Welcome back, Bob.' }).count(), 0);
  assert.equal(await apiStatus(bob, '/me'), 200);
  assert.equal(await apiStatus(bob, '/auth/logout', 'post'), 200);
  assert.equal(await apiStatus(bob, '/me'), 401);
  await context.addCookies([bobCookie]);
  assert.equal(await apiStatus(bob, '/me'), 401, 'a copied cookie stays revoked after logout');
  console.log('Browser session isolation and logout passed with synthetic Telegram data and an in-memory fixture.');
} finally {
  await browser.close();
}
