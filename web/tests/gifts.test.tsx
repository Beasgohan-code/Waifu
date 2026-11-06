import { render, screen, waitFor } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { afterEach, expect, it, vi } from 'vitest';
import Gifts from '../src/pages/Gifts';
import type { Profile } from '../src/types';

const profile: Profile = { id: 1, first_name: 'Mika', username: null, points: 0, xp: 0, level: 1, catch_count: 1, unique_characters: 1, catalog_size: 1, streak: 0, last_daily: null, is_operator: false };
const reply = (data: unknown) => new Response(JSON.stringify({ data }), { status: 200, headers: { 'Content-Type': 'application/json' } });
afterEach(() => vi.unstubAllGlobals());

it('offers an owned character using the authenticated session, not a supplied sender ID', async () => {
  const fetcher = vi.fn((url: string, options: RequestInit) => {
    if (options.method === 'POST') return Promise.resolve(reply({ id: 'gift' }));
    if (url.startsWith('/api/inventory')) return Promise.resolve(reply({ items: [{ id: 'aiko', name: 'Aiko' }], total: 1, page: 1, limit: 50 }));
    return Promise.resolve(reply([]));
  });
  vi.stubGlobal('fetch', fetcher);
  const refresh = vi.fn(); const notify = vi.fn();
  const user = userEvent.setup();
  render(<Gifts profile={profile} preview={false} revision={0} refresh={refresh} notify={notify} />);
  const button = await screen.findByRole('button', { name: 'New gift' });
  await waitFor(() => expect(button).toBeEnabled());
  await user.click(button);
  await user.type(screen.getByLabelText("Recipient's Telegram user ID"), '456');
  await user.selectOptions(screen.getByLabelText('Your character'), 'aiko');
  await user.click(screen.getByRole('button', { name: 'Offer gift' }));
  await waitFor(() => expect(refresh).toHaveBeenCalledOnce());
  const post = fetcher.mock.calls.find(([url, options]) => url === '/api/gifts' && options.method === 'POST');
  expect(post).toBeDefined();
  if (!post) throw new Error('Gift request was not sent');
  const [, options] = post;
  expect(JSON.parse(options.body as string)).toEqual({ receiver_id: 456, character_id: 'aiko' });
  expect(new Headers(options.headers).get('X-Summon-Request')).toBe('1');
});
