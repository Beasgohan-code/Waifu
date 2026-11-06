import type { Profile } from './types';

export class ApiError extends Error {
  constructor(public status: number, message: string) { super(message); this.name = 'ApiError'; }
}

type Envelope<T> = { data: T; error?: { message: string } };

// This is a non-reversible launch identifier, not the Telegram proof or an identity.
// Keep it in page memory so each tab uses its own launch instead of only shared cookies.
let launchDigest: string | null = null;

async function request<T>(path: string, method = 'GET', body?: unknown): Promise<T> {
  if (!path.startsWith('/') || path.startsWith('//')) throw new Error('Only same-origin API paths are allowed');
  const headers: Record<string, string> = launchDigest ? { 'X-Summon-Launch': launchDigest } : {};
  if (method !== 'GET') {
    headers['X-Summon-Request'] = '1';
    headers['Content-Type'] = 'application/json';
  }
  const response = await fetch(`/api${path}`, {
    method, headers, credentials: 'same-origin', ...(body !== undefined ? { body: JSON.stringify(body) } : {}),
  });
  let payload: Partial<Envelope<T>> = {};
  try { payload = await response.json() as Partial<Envelope<T>>; } catch { /* A gateway may return non-JSON. */ }
  if (!response.ok) throw new ApiError(response.status, payload.error?.message || 'Something went wrong. Please try again.');
  if (!('data' in payload)) throw new ApiError(502, 'The server returned an unexpected response.');
  return payload.data as T;
}

export const api = {
  get: <T>(path: string) => request<T>(path),
  post: <T>(path: string, body?: unknown) => request<T>(path, 'POST', body),
  put: <T>(path: string, body: unknown) => request<T>(path, 'PUT', body),
  patch: <T>(path: string, body: unknown) => request<T>(path, 'PATCH', body),
};

let pendingSession: { initData: string; promise: Promise<Profile> } | null = null;

async function fingerprintLaunch(initData: string): Promise<string> {
  const proofs = new URLSearchParams(initData).getAll('hash');
  if (proofs.length !== 1 || !/^[0-9a-f]{64}$/.test(proofs[0]) || !globalThis.crypto?.subtle) {
    throw new ApiError(401, 'Invalid Telegram launch data; reopen SUMMON in Telegram.');
  }
  const digest = await globalThis.crypto.subtle.digest('SHA-256', new TextEncoder().encode(proofs[0]));
  return Array.from(new Uint8Array(digest), byte => byte.toString(16).padStart(2, '0')).join('');
}

export function startSession(initData: string): Promise<Profile> {
  // StrictMode may request the same sign-in twice; never coalesce different identities.
  if (pendingSession) {
    if (pendingSession.initData === initData) return pendingSession.promise;
    return Promise.reject(new ApiError(409, 'Another Telegram sign-in is in progress; reopen SUMMON.'));
  }
  const work = (async () => {
    launchDigest = null;
    if (initData) launchDigest = await fingerprintLaunch(initData);
    try { return await api.get<Profile>('/me'); }
    catch (error) {
      if (!(error instanceof ApiError) || error.status !== 401 || !initData) throw error;
    }
    await api.post('/auth/session', { init_data: initData });
    return api.get<Profile>('/me');
  })();
  const promise = work.finally(() => { pendingSession = null; });
  pendingSession = { initData, promise };
  return promise;
}
