// Read-only end-to-end smoke test against the explicitly configured sample preview.
// Start the preview servers first; set CHROMIUM_PATH if Chromium isn't in PATH.
import assert from 'node:assert/strict';
import { chromium } from 'playwright-core';

const url = process.env.SUMMON_PREVIEW_URL || 'http://127.0.0.1:5173';
const executablePath = process.env.CHROMIUM_PATH;
const browser = await chromium.launch({
  headless: true,
  ...(executablePath ? { executablePath } : {}),
  args: ['--no-sandbox', '--disable-dev-shm-usage'],
});
const page = await browser.newPage({ viewport: { width: 1440, height: 1000 } });
// The preview is synthetic; do not depend on the live Telegram bridge or its defaults.
await page.route('https://telegram.org/js/telegram-web-app.js', route => route.fulfill({ contentType: 'application/javascript', body: '' }));
await page.addInitScript(() => {
  const state = { visible: false, params: null, callbacks: new Set() };
  window.__summonNativeButton = state;
  window.Telegram = { WebApp: {
    initData: '', isVersionAtLeast: version => version === '9.5',
    MainButton: {
      setParams: params => { state.params = params; },
      onClick: callback => state.callbacks.add(callback),
      offClick: callback => state.callbacks.delete(callback),
      show: () => { state.visible = true; },
      hide: () => { state.visible = false; },
    },
  } };
});
const errors = [];
const writes = [];
page.on('pageerror', error => errors.push(error.message));
page.on('request', request => {
  if (new URL(request.url()).pathname.startsWith('/api/') && request.method() !== 'GET') writes.push(request.method());
});

try {
  await page.goto(url, { waitUntil: 'networkidle' });
  await page.getByText('Read-only preview', { exact: false }).first().waitFor();
  await page.waitForFunction(() => window.__summonNativeButton.visible);
  assert.deepEqual(await page.evaluate(() => window.__summonNativeButton.params), {
    text: 'Open collection', icon_custom_emoji_id: '6215348101478097302',
  });
  assert.equal(await page.locator('.character-card').count(), 3);
  const opener = page.getByRole('button', { name: 'View Aiko Tsukino' });
  await opener.click();
  await page.getByRole('dialog', { name: 'Character details' }).waitFor();
  await page.waitForFunction(() => !window.__summonNativeButton.visible && window.__summonNativeButton.callbacks.size === 0);
  await page.getByText('Sample character · actions are disabled in preview.').waitFor();
  assert.equal(await page.getByRole('button', { name: 'Favorited' }).isDisabled(), true);
  await page.keyboard.press('Shift+Tab');
  assert.equal(await page.getByRole('dialog').evaluate(element => element.contains(document.activeElement)), true, 'dialog traps focus');
  await page.keyboard.press('Tab');
  assert.equal(await page.getByRole('dialog').evaluate(element => element.contains(document.activeElement)), true, 'dialog traps focus');
  await page.keyboard.press('Escape');
  await page.getByRole('dialog').waitFor({ state: 'hidden' });
  assert.equal(await opener.evaluate(element => document.activeElement === element), true, 'dialog restores focus');
  await page.waitForFunction(() => window.__summonNativeButton.visible && window.__summonNativeButton.callbacks.size === 1);
  await page.evaluate(() => window.__summonNativeButton.callbacks.forEach(callback => callback()));
  await page.getByRole('heading', { name: 'Your collection.' }).waitFor();
  await page.waitForFunction(() => !window.__summonNativeButton.visible && window.__summonNativeButton.callbacks.size === 0);
  await page.getByRole('button', { name: 'Overview', exact: true }).click();

  await page.getByRole('button', { name: 'Discover', exact: true }).first().click();
  await page.getByRole('heading', { name: 'Meet the characters.' }).waitFor();
  await page.getByLabel('Search characters by name').fill('Ren');
  await page.getByText('1 character', { exact: true }).waitFor();
  assert.equal(await page.locator('.character-card').count(), 1);
  assert.equal(await page.getByRole('button', { name: 'View Ren Aster' }).count(), 1);

  await page.getByRole('button', { name: 'My collection', exact: true }).first().click();
  await page.getByRole('button', { name: 'Favorites', exact: true }).click();
  await page.getByText('1 character', { exact: true }).waitFor();
  assert.equal(await page.getByRole('button', { name: 'View Aiko Tsukino' }).count(), 1);
  await page.getByRole('button', { name: 'Duplicates', exact: true }).click();
  await page.getByRole('button', { name: 'View Ren Aster' }).waitFor();
  assert.equal(await page.getByRole('button', { name: 'View Aiko Tsukino' }).count(), 0);
  await page.getByLabel('Sort collection').selectOption('copies');
  await page.getByRole('button', { name: 'All characters', exact: true }).click();
  await page.getByRole('button', { name: 'View Aiko Tsukino' }).waitFor();
  assert.equal(await page.locator('.character-card').first().getByRole('button', { name: 'View Ren Aster' }).count(), 1);

  await page.getByRole('button', { name: 'Group manager' }).click();
  await page.getByRole('heading', { name: "The Collector's Club" }).waitFor();
  await page.getByRole('tab', { name: 'Protection' }).click();
  assert.equal(await page.getByRole('button', { name: 'Save protection settings' }).isDisabled(), true);
  await page.getByRole('tab', { name: 'Welcome & rules' }).click();
  assert.equal(await page.getByRole('checkbox', { name: /Say goodbye to members/ }).isDisabled(), true);
  assert.equal(await page.getByLabel('Goodbye message').isDisabled(), true);
  assert.equal(await page.getByRole('button', { name: 'Save community settings' }).isDisabled(), true);
  await page.getByRole('tab', { name: 'Notes' }).click();
  await page.getByText('Sample data only · notes cannot be changed in this preview.').waitFor();
  assert.equal(await page.getByLabel('Note text').inputValue(), 'Say hello, introduce yourself, and share your favorite character!');
  assert.ok(await page.getByLabel('Note text').isDisabled());
  assert.ok(await page.getByRole('button', { name: 'Save note' }).isDisabled());
  assert.ok(await page.getByRole('button', { name: 'Delete note' }).isDisabled());
  await page.getByRole('tab', { name: 'Moderation' }).click();
  assert.equal(await page.getByRole('button', { name: 'Apply action' }).isDisabled(), true);
  assert.equal(await page.getByLabel('Member ID to review').inputValue(), '456');
  await page.getByRole('button', { name: 'Find warnings' }).click();
  await page.getByText('Sample warning for review').waitFor();
  assert.equal(await page.getByRole('button', { name: 'Revoke warning' }).isDisabled(), true);

  await page.getByRole('button', { name: 'Trading', exact: true }).click();
  await page.getByRole('heading', { name: 'Trade with friends.' }).waitFor();
  assert.equal(await page.getByRole('button', { name: 'New trade' }).isDisabled(), true);
  await page.getByRole('button', { name: 'Gifts', exact: true }).click();
  await page.getByRole('heading', { name: 'A gift from the heart.' }).waitFor();
  await page.getByRole('heading', { name: 'No gifts yet' }).waitFor();
  assert.equal(await page.getByRole('button', { name: 'New gift' }).isDisabled(), true);
  await page.getByRole('button', { name: 'Achievements', exact: true }).click();
  await page.getByRole('heading', { name: 'Your journey.' }).waitFor();
  await page.getByRole('button', { name: 'Leaderboard', exact: true }).click();
  await page.getByRole('heading', { name: 'Made for sharing.' }).waitFor();
  await page.getByRole('button', { name: 'Settings', exact: true }).click();
  await page.getByRole('heading', { name: 'Settings.' }).waitFor();
  await page.getByRole('button', { name: 'Light', exact: true }).click();
  assert.equal(await page.locator('html').getAttribute('data-theme'), 'light');
  assert.equal(await page.evaluate(() => localStorage.getItem('summon-theme')), 'light');
  await page.getByRole('button', { name: 'Follow Telegram' }).click();
  assert.equal(await page.evaluate(() => localStorage.getItem('summon-theme')), null);

  for (const width of [390, 320]) {
    await page.setViewportSize({ width, height: 844 });
    await page.reload({ waitUntil: 'networkidle' });
    const mobileNav = page.getByRole('navigation', { name: 'Mobile navigation' });
    assert.ok(await mobileNav.isVisible());
    assert.ok(await page.evaluate(() => document.body.scrollWidth <= window.innerWidth), `no horizontal overflow at ${width}px`);
    if (width === 320) await page.getByRole('button', { name: /Switch to (light|dark) theme/ }).click();
    await mobileNav.getByRole('button', { name: 'Collection', exact: true }).click();
    const duplicatesButton = page.getByRole('button', { name: 'Duplicates', exact: true });
    assert.ok((await duplicatesButton.boundingBox()).height >= 44, 'duplicate filter has a 44px touch target');
    await duplicatesButton.click();
    await page.getByRole('button', { name: 'View Ren Aster' }).waitFor();
    await page.getByLabel('Sort collection').selectOption('copies');
    assert.ok(await page.evaluate(() => document.body.scrollWidth <= window.innerWidth), `collection filters fit ${width}px`);
    await mobileNav.getByRole('button', { name: 'Groups', exact: true }).click();
    await page.getByRole('tab', { name: 'Welcome & rules' }).click();
    assert.ok(await page.getByLabel('Goodbye message').isDisabled());
    assert.ok(await page.evaluate(() => document.body.scrollWidth <= window.innerWidth), `community settings fit ${width}px`);
    await page.getByRole('tab', { name: 'Notes' }).click();
    await page.getByText('Sample data only · notes cannot be changed in this preview.').waitFor();
    assert.ok(await page.getByLabel('Note text').isDisabled());
    assert.ok(await page.getByRole('button', { name: 'Save note' }).isDisabled());
    assert.ok(await page.evaluate(() => document.body.scrollWidth <= window.innerWidth), `group notes fit ${width}px`);
    await page.getByRole('tab', { name: 'Auto replies' }).click();
    await page.getByText('Sample data only · auto replies cannot be changed in this preview.').waitFor();
    assert.ok(await page.getByLabel('Reply text').isDisabled());
    assert.ok(await page.getByRole('button', { name: 'Save auto reply' }).isDisabled());
    assert.ok(await page.evaluate(() => document.body.scrollWidth <= window.innerWidth), `auto replies fit ${width}px`);
    await page.getByRole('tab', { name: 'Moderation' }).click();
    await page.getByRole('button', { name: 'Find warnings' }).click();
    await page.getByText('Sample warning for review').waitFor();
    assert.ok(await page.getByRole('button', { name: 'Revoke warning' }).isDisabled());
    assert.ok(await page.evaluate(() => document.body.scrollWidth <= window.innerWidth), `warning review fits ${width}px`);
    await page.getByRole('button', { name: 'More navigation' }).click();
    await page.locator('.mobile-more').getByRole('button', { name: 'Trading' }).click();
    await page.getByRole('heading', { name: 'Trade with friends.' }).waitFor();
    assert.ok(await page.evaluate(() => document.body.scrollWidth <= window.innerWidth), `trade page fits ${width}px`);
  }
  await page.route('**/api/bootstrap', route => route.fulfill({ json: { data: {
    preview: true, collection_button_emoji: { fallback: '💎', custom_emoji_id: null },
  } } }));
  const homeUrl = new URL(url);
  homeUrl.hash = 'home';
  await page.goto(homeUrl.toString(), { waitUntil: 'networkidle' });
  // Changing only the hash does not remount the app or re-fetch bootstrap.
  await page.reload({ waitUntil: 'networkidle' });
  await page.waitForFunction(() => window.__summonNativeButton.visible);
  assert.deepEqual(await page.evaluate(() => window.__summonNativeButton.params), {
    text: '💎 Open collection', icon_custom_emoji_id: '',
  });
  assert.deepEqual(writes, []);
  assert.deepEqual(errors, []);
  console.log('Browser smoke passed: native shortcut mock, desktop navigation, search, dialogs, preview safety, themes, and 390/320px mobile.');
} finally {
  await browser.close();
}
