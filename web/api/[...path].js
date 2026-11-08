// Same-origin API proxy for a Vercel-hosted Mini App and an HTTPS Render API.
// The upstream is operator-configured, never derived from the request.
import { request as httpsRequest } from 'node:https';

export const config = { api: { bodyParser: false } };

const MAX_BODY = 64 * 1024;
const METHODS = new Set(['GET', 'HEAD', 'POST', 'PUT', 'PATCH', 'DELETE']);
const REQUEST_HEADERS = ['accept', 'content-type', 'cookie', 'origin', 'x-summon-launch', 'x-summon-request'];
const RESPONSE_HEADERS = ['cache-control', 'content-length', 'content-type', 'etag', 'last-modified', 'referrer-policy', 'retry-after', 'set-cookie', 'vary', 'x-content-type-options'];

function sendError(res, status, message) {
  if (res.headersSent) { res.destroy(); return; }
  res.statusCode = status;
  res.setHeader('Content-Type', 'application/json');
  res.setHeader('Cache-Control', 'no-store');
  res.end(JSON.stringify({ error: { code: 'proxy_unavailable', message } }));
}

function targetFor(requestUrl, origin) {
  const base = new URL(origin);
  if (base.protocol !== 'https:' || base.username || base.password || base.pathname !== '/' || base.search || base.hash) {
    throw new Error('Invalid backend origin');
  }
  // A request cannot choose a host or escape the API namespace.
  if (typeof requestUrl !== 'string' || !requestUrl.startsWith('/api/')) throw new Error('Invalid API path');
  const incoming = new URL(requestUrl, 'https://frontend.invalid');
  if (!incoming.pathname.startsWith('/api/') || incoming.hash) throw new Error('Invalid API path');
  return new URL(incoming.pathname + incoming.search, base);
}

async function bodyFor(req) {
  const chunks = [];
  let size = 0;
  for await (const chunk of req) {
    const bytes = Buffer.isBuffer(chunk) ? chunk : Buffer.from(chunk);
    size += bytes.length;
    if (size > MAX_BODY) throw new RangeError('Request body is too large');
    chunks.push(bytes);
  }
  return Buffer.concat(chunks);
}

export default async function handler(req, res) {
  if (!METHODS.has(req.method)) return sendError(res, 405, 'Method not allowed');
  let target;
  try {
    target = targetFor(req.url, process.env.SUMMON_BACKEND_ORIGIN || '');
  } catch {
    return sendError(res, 503, 'API proxy is not configured');
  }
  let body;
  try {
    body = await bodyFor(req);
  } catch (error) {
    return sendError(res, error instanceof RangeError ? 413 : 400, 'Invalid request body');
  }
  const headers = {};
  for (const name of REQUEST_HEADERS) if (req.headers[name] !== undefined) headers[name] = req.headers[name];
  if (body.length) headers['content-length'] = String(body.length);
  // Never forward untrusted Host, forwarding headers, or hop-by-hop headers.
  await new Promise(resolve => {
    const upstream = httpsRequest(target, { method: req.method, headers, timeout: 30000 }, response => {
      res.statusCode = response.statusCode || 502;
      for (const name of RESPONSE_HEADERS) if (response.headers[name] !== undefined) res.setHeader(name, response.headers[name]);
      response.on('error', () => res.destroy());
      response.pipe(res);
    });
    upstream.on('timeout', () => upstream.destroy(new Error('Upstream timeout')));
    upstream.on('error', () => { sendError(res, 502, 'API temporarily unavailable'); resolve(); });
    res.on('finish', resolve);
    res.on('close', () => { if (!res.writableEnded) upstream.destroy(); resolve(); });
    upstream.end(body);
  });
}
