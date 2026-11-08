// Synthetic cross-site Telegram Web embedding test through the actual Vercel
// proxy handler and a local HTTPS upstream. Uses ONLY the isolated fixture.
import assert from 'node:assert/strict';
import { execFileSync } from 'node:child_process';
import { createHmac, createHash, randomUUID } from 'node:crypto';
import { createServer as createHttpsServer, globalAgent as httpsAgent } from 'node:https';
import { request as httpRequest } from 'node:http';
import { mkdtempSync, readFileSync, rmSync } from 'node:fs';
import { tmpdir } from 'node:os';
import { extname, join, resolve, sep } from 'node:path';
import { chromium } from 'playwright-core';
import proxy from '../api/[...path].js';

const frontend = 'https://app.summon.test:19443';
const parent = 'https://parent.telegram.test:19443';
const otherParent = 'https://other-messenger.test:19443';
const dist = resolve('dist');
const scratch = mkdtempSync(join(tmpdir(), 'summon-embedded-browser-'));
const token = '123456:TEST_TOKEN_DO_NOT_USE';
const fields = {
  auth_date: String(Math.floor(Date.now() / 1000)),
  query_id: randomUUID(),
  user: JSON.stringify({ id: 987, first_name: 'Embedded' }),
};
const check = Object.entries(fields).sort(([a], [b]) => a.localeCompare(b)).map(([key, value]) => `${key}=${value}`).join('\n');
const secret = createHmac('sha256', 'WebAppData').update(token).digest();
fields.hash = createHmac('sha256', secret).update(check).digest('hex');
const initData = new URLSearchParams(fields).toString();
const launchDigest = createHash('sha256').update(fields.hash).digest('hex');

execFileSync('openssl', [
  'req', '-x509', '-newkey', 'rsa:2048', '-nodes', '-keyout', join(scratch, 'key.pem'),
  '-out', join(scratch, 'cert.pem'), '-days', '1', '-subj', '/CN=app.summon.test',
  '-addext', 'subjectAltName=DNS:app.summon.test,DNS:parent.telegram.test,DNS:other-messenger.test,IP:127.0.0.1',
], { stdio: 'ignore' });

const tls = { key: readFileSync(join(scratch, 'key.pem')), cert: readFileSync(join(scratch, 'cert.pem')) };
const previousCA = httpsAgent.options.ca;
const previousBackend = process.env.SUMMON_BACKEND_ORIGIN;
// Trust only the test certificate for this local upstream; TLS verification
// remains enabled. Neither the certificate nor the override is used in production.
httpsAgent.options.ca = tls.cert;
process.env.SUMMON_BACKEND_ORIGIN = 'https://127.0.0.1:19444';
let upstreamRequests = 0;
let lastUpstreamHeaders = {};
const backend = createHttpsServer(tls, (req, res) => {
  upstreamRequests += 1;
  lastUpstreamHeaders = req.headers;
  const upstream = httpRequest({ host: '127.0.0.1', port: 8000, path: req.url, method: req.method, headers: req.headers }, response => {
    res.writeHead(response.statusCode || 502, response.headers);
    response.pipe(res);
  });
  upstream.on('error', () => { if (!res.headersSent) res.writeHead(502); res.end(); });
  req.pipe(upstream);
});
const mime = { '.html': 'text/html', '.js': 'text/javascript', '.css': 'text/css', '.svg': 'image/svg+xml', '.woff': 'font/woff', '.woff2': 'font/woff2' };
const server = createHttpsServer(tls, (req, res) => {
  const host = (req.headers.host || '').split(':')[0];
  const path = new URL(req.url || '/', frontend).pathname;
  if (host === 'parent.telegram.test' || host === 'other-messenger.test') {
    res.writeHead(200, { 'Content-Type': 'text/html' });
    res.end(`<title>Synthetic Telegram Web</title><iframe title="SUMMON" src="${frontend}/"></iframe>`);
    return;
  }
  if (host !== 'app.summon.test') { res.writeHead(404); res.end(); return; }
  if (path.startsWith('/api/')) {
    // Invoke the same handler used by Vercel; routing on the hosted platform
    // still requires separate verification.
    void proxy(req, res).catch(() => { if (!res.headersSent) res.writeHead(500); res.end(); });
    return;
  }
  const filename = resolve(dist, `.${path === '/' ? '/index.html' : path}`);
  if (!filename.startsWith(dist + sep)) { res.writeHead(404); res.end(); return; }
  try {
    const bytes = readFileSync(filename);
    res.writeHead(200, { 'Content-Type': mime[extname(filename)] || 'application/octet-stream' });
    res.end(bytes);
  } catch { res.writeHead(404); res.end(); }
});

const listen = (instance, port) => new Promise((resolveListen, rejectListen) => instance.once('error', rejectListen).listen(port, '127.0.0.1', resolveListen));
const close = instance => new Promise(resolveClose => instance.listening ? instance.close(resolveClose) : resolveClose());
let browser;
try {
  await listen(backend, 19444);
  await listen(server, 19443);
  browser = await chromium.launch({
    headless: true,
    ...(process.env.CHROMIUM_PATH ? { executablePath: process.env.CHROMIUM_PATH } : {}),
    args: [
      '--no-sandbox', '--disable-dev-shm-usage', '--no-proxy-server',
      '--host-resolver-rules=MAP app.summon.test 127.0.0.1, MAP parent.telegram.test 127.0.0.1, MAP other-messenger.test 127.0.0.1',
      '--test-third-party-cookie-phaseout',
    ],
  });
  const context = await browser.newContext({ ignoreHTTPSErrors: true });
  await context.route('https://telegram.org/js/telegram-web-app.js', route => route.fulfill({ body: '', contentType: 'text/javascript' }));
  await context.addInitScript(data => {
    window.Telegram = { WebApp: { initData: data, ready() {}, expand() {}, onEvent() {}, offEvent() {} } };
  }, initData);
  const page = await context.newPage();
  let signIns = 0;
  page.on('request', request => { if (request.method() === 'POST' && request.url().endsWith('/api/auth/session')) signIns += 1; });
  await page.goto(parent, { waitUntil: 'networkidle' });
  await page.frameLocator('iframe').getByRole('heading', { name: 'Welcome back, Embedded.' }).waitFor();
  const cookies = await context.cookies();
  const session = cookies.find(cookie => cookie.name === 'summon_session_v2');
  assert.ok(session, 'the cross-site frame has a session cookie');
  assert.equal(session.httpOnly, true);
  assert.equal(session.secure, true);
  assert.equal(session.sameSite, 'None');
  assert.ok(session.partitionKey && String(session.partitionKey).includes('telegram.test'), 'the cookie is partitioned by the parent site');
  assert.equal(signIns, 1);

  // A reload in the same embedding can resume without replaying the one-time proof.
  await page.reload({ waitUntil: 'networkidle' });
  await page.frameLocator('iframe').getByRole('heading', { name: 'Welcome back, Embedded.' }).waitFor();
  assert.equal(signIns, 1);
  const frame = page.frames().find(item => item.url().startsWith(frontend));
  assert.ok(frame);
  const me = await frame.evaluate(async digest => (await fetch('/api/me', { headers: {
    'X-Summon-Launch': digest, 'X-Forwarded-For': '203.0.113.9', 'X-Forwarded-Host': 'spoofed.invalid',
  }, credentials: 'same-origin' })).status, launchDigest);
  assert.equal(me, 200);
  assert.equal(lastUpstreamHeaders['x-forwarded-for'], undefined);
  assert.equal(lastUpstreamHeaders['x-forwarded-host'], undefined);
  assert.equal(lastUpstreamHeaders.host, '127.0.0.1:19444');
  const priorUpstreamRequests = upstreamRequests;
  const oversized = await frame.evaluate(async digest => (await fetch('/api/auth/session', {
    method: 'POST', credentials: 'same-origin', headers: { 'X-Summon-Request': '1', 'X-Summon-Launch': digest }, body: 'x'.repeat(65_537),
  })).status, launchDigest);
  assert.equal(oversized, 413);
  assert.equal(upstreamRequests, priorUpstreamRequests, 'oversized bodies must not reach the backend');

  // Another top-level site cannot reuse this partition, even in the same
  // browser context. The already-consumed launch proof is not accepted there.
  const otherPage = await context.newPage();
  await otherPage.goto(otherParent, { waitUntil: 'networkidle' });
  await otherPage.frameLocator('iframe').getByText('Telegram launch data has already been used; reopen SUMMON').waitFor();
  assert.equal((await context.cookies()).filter(cookie => cookie.name === 'summon_session_v2').length, 1);
  assert.equal(await frame.evaluate(async digest => (await fetch('/api/me', { headers: { 'X-Summon-Launch': digest }, credentials: 'same-origin' })).status, launchDigest), 200);
  await otherPage.close();

  // Parent-origin JavaScript cannot use the cross-site cookie to bypass CSRF.
  const attack = await page.evaluate(async ({ origin, digest }) => {
    try {
      const response = await fetch(`${origin}/api/auth/logout`, {
        method: 'POST', credentials: 'include', headers: { 'X-Summon-Request': '1', 'X-Summon-Launch': digest },
      });
      return response.status;
    } catch { return 0; } // CORS preflight rejection is also a safe result.
  }, { origin: frontend, digest: launchDigest });
  assert.ok(attack === 0 || attack === 403, `cross-site logout rejected (${attack})`);
  assert.equal(await frame.evaluate(async digest => (await fetch('/api/me', { headers: { 'X-Summon-Launch': digest }, credentials: 'same-origin' })).status, launchDigest), 200);
  const logout = await frame.evaluate(async digest => (await fetch('/api/auth/logout', {
    method: 'POST', headers: { 'X-Summon-Request': '1', 'X-Summon-Launch': digest }, credentials: 'same-origin',
  })).status, launchDigest);
  assert.equal(logout, 200);
  assert.equal(await frame.evaluate(async digest => (await fetch('/api/me', { headers: { 'X-Summon-Launch': digest }, credentials: 'same-origin' })).status, launchDigest), 401);
  assert.ok(!(await context.cookies()).some(cookie => cookie.name === 'summon_session_v2'), 'logout clears the partitioned cookie');
  console.log('Synthetic HTTPS embedded sign-in, partitioning, CSRF rejection, reload and logout passed.');
} finally {
  await browser?.close();
  await close(server);
  await close(backend);
  httpsAgent.options.ca = previousCA;
  if (previousBackend === undefined) delete process.env.SUMMON_BACKEND_ORIGIN;
  else process.env.SUMMON_BACKEND_ORIGIN = previousBackend;
  rmSync(scratch, { recursive: true, force: true });
}
