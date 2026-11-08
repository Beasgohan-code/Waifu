import { createHash, webcrypto } from 'node:crypto';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import { ApiError, api, startSession } from '../src/api';

const reply = (data: unknown, status = 200) => new Response(JSON.stringify(data), {
  status, headers: { 'Content-Type': 'application/json' },
});
const launchA = `auth_date=1&hash=${'a'.repeat(64)}`;
const launchB = `auth_date=1&hash=${'b'.repeat(64)}`;
const digest = (letter: string) => createHash('sha256').update(letter.repeat(64)).digest('hex');

beforeEach(() => vi.stubGlobal('crypto', webcrypto));
afterEach(() => vi.unstubAllGlobals());

describe('Telegram launch isolation', () => {
  it('uses the current launch fingerprint when an old account cookie exists', async () => {
    const fetcher = vi.fn()
      .mockResolvedValueOnce(reply({ error: { message: 'Different launch' } }, 401))
      .mockResolvedValueOnce(reply({ data: { first_name: 'B' } }))
      .mockResolvedValueOnce(reply({ data: { id: 456, first_name: 'B' } }))
      .mockResolvedValueOnce(reply({ data: [] }));
    vi.stubGlobal('fetch', fetcher);
    const user = await startSession(launchB);
    expect(user.id).toBe(456);
    expect(fetcher.mock.calls.map(call => call[0])).toEqual(['/api/me', '/api/auth/session', '/api/me']);
    for (const [, options] of fetcher.mock.calls) expect(options.headers['X-Summon-Launch']).toBe(digest('b'));
    expect(fetcher.mock.calls[1][1].body).toBe(JSON.stringify({ init_data: launchB }));
    await api.get('/inventory');
    expect(fetcher.mock.calls[3][1].headers['X-Summon-Launch']).toBe(digest('b'));
  });

  it('resumes only a server-approved matching launch without consuming its proof again', async () => {
    const fetcher = vi.fn().mockResolvedValue(reply({ data: { id: 123, first_name: 'A' } }));
    vi.stubGlobal('fetch', fetcher);
    expect((await startSession(launchA)).id).toBe(123);
    expect(fetcher).toHaveBeenCalledTimes(1);
    expect(fetcher.mock.calls[0][1].headers['X-Summon-Launch']).toBe(digest('a'));
  });

  it('does not fall back to an old cookie when the current launch is rejected', async () => {
    const fetcher = vi.fn()
      .mockResolvedValueOnce(reply({ error: { message: 'Different launch' } }, 401))
      .mockResolvedValueOnce(reply({ error: { message: 'Invalid launch' } }, 401));
    vi.stubGlobal('fetch', fetcher);
    await expect(startSession(launchB)).rejects.toBeInstanceOf(ApiError);
    expect(fetcher.mock.calls.map(call => call[0])).toEqual(['/api/me', '/api/auth/session']);
  });

  it('rejects different simultaneous launch proofs instead of sharing the first identity', async () => {
    let resolveFirst!: (value: Response) => void;
    const fetcher = vi.fn().mockImplementationOnce(() => new Promise<Response>(resolve => { resolveFirst = resolve; }));
    vi.stubGlobal('fetch', fetcher);
    const first = startSession(launchA);
    let secondError: unknown;
    const second = startSession(launchB).catch(error => { secondError = error; return null; });
    // Wait for the asynchronous digest to complete and the GET to start.
    await vi.waitFor(() => expect(fetcher).toHaveBeenCalledTimes(1));
    resolveFirst(reply({ data: { id: 123, first_name: 'A' } }));
    expect((await first).id).toBe(123);
    await second;
    expect(secondError).toBeInstanceOf(ApiError);
  });

  it('does not send private requests for malformed or duplicated launch hashes', async () => {
    const fetcher = vi.fn();
    vi.stubGlobal('fetch', fetcher);
    await expect(startSession('hash=bad')).rejects.toBeInstanceOf(ApiError);
    await expect(startSession(`${launchA}&hash=${'a'.repeat(64)}`)).rejects.toBeInstanceOf(ApiError);
    expect(fetcher).not.toHaveBeenCalled();
  });

  it('fails closed when Web Crypto is unavailable', async () => {
    vi.stubGlobal('crypto', {});
    const fetcher = vi.fn();
    vi.stubGlobal('fetch', fetcher);
    await expect(startSession(launchA)).rejects.toBeInstanceOf(ApiError);
    expect(fetcher).not.toHaveBeenCalled();
  });
});
