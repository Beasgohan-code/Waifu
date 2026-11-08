import { webcrypto } from 'node:crypto';
import { afterEach, describe, expect, it, vi } from 'vitest';
import { ApiError, api, startSession } from '../src/api';

const reply = (data: unknown, status = 200) => new Response(JSON.stringify(data), {
  status, headers: { 'Content-Type': 'application/json' },
});

afterEach(() => vi.unstubAllGlobals());

describe('same-origin API boundary', () => {
  it('uses relative paths and a CSRF header for writes', async () => {
    const fetcher = vi.fn().mockResolvedValue(reply({ data: { points: 100 } }));
    vi.stubGlobal('fetch', fetcher);
    await api.post('/daily', {});
    const [url, options] = fetcher.mock.calls[0];
    expect(url).toBe('/api/daily');
    expect(options.credentials).toBe('same-origin');
    expect(options.headers['X-Summon-Request']).toBe('1');
  });

  it('reports server errors without pretending a request succeeded', async () => {
    vi.stubGlobal('fetch', vi.fn().mockResolvedValue(reply({ error: { message: 'Sign in required' } }, 401)));
    await expect(api.get('/me')).rejects.toBeInstanceOf(ApiError);
  });

  it('does not send an unsigned browser user identity when Telegram data is absent', async () => {
    const fetcher = vi.fn().mockResolvedValue(reply({ error: { message: 'Sign in required' } }, 401));
    vi.stubGlobal('fetch', fetcher);
    await expect(startSession('')).rejects.toBeInstanceOf(ApiError);
    expect(fetcher).toHaveBeenCalledTimes(1);
    expect(fetcher.mock.calls[0][0]).toBe('/api/me');
  });
});

it('coalesces concurrent sign-ins so the one-time Telegram proof is not replayed', async () => {
  vi.stubGlobal('crypto', webcrypto);
  const fetcher = vi.fn()
    .mockResolvedValueOnce(reply({ error: { message: 'Sign in' } }, 401))
    .mockResolvedValueOnce(reply({ data: { first_name: 'Mika' } }))
    .mockResolvedValueOnce(reply({ data: { id: 123, first_name: 'Mika' } }));
  vi.stubGlobal('fetch', fetcher);
  const launch = `hash=${'a'.repeat(64)}`;
  const [first, second] = await Promise.all([startSession(launch), startSession(launch)]);
  expect(first.id).toBe(123);
  expect(second.id).toBe(123);
  expect(fetcher.mock.calls.filter(call => call[0] === '/api/auth/session')).toHaveLength(1);
});

it('explains an unavailable cookie after sign-in without replaying the one-time launch', async () => {
  vi.stubGlobal('crypto', webcrypto);
  const fetcher = vi.fn()
    .mockResolvedValueOnce(reply({ error: { message: 'Sign in required' } }, 401))
    .mockResolvedValueOnce(reply({ data: { first_name: 'Mika' } }))
    .mockResolvedValueOnce(reply({ error: { message: 'Sign in required' } }, 401));
  vi.stubGlobal('fetch', fetcher);
  await expect(startSession(`hash=${'b'.repeat(64)}`)).rejects.toThrow(/Cookies may be blocked.*reopen SUMMON/);
  expect(fetcher.mock.calls.map(call => call[0])).toEqual(['/api/me', '/api/auth/session', '/api/me']);
});
