import { cleanup, fireEvent, render, screen, waitFor } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { afterEach, expect, it, vi } from 'vitest';
import Home from '../src/pages/Home';
import Progress from '../src/pages/Progress';
import type { Profile } from '../src/types';

const profile: Profile = {
  id: 1, first_name: 'Test', username: null, points: 0, xp: 0, level: 1,
  catch_count: 0, unique_characters: 0, catalog_size: 0, streak: 0,
  last_daily: null, is_operator: false,
};
const response = (data: unknown, status = 200) => new Response(JSON.stringify({ data }), {
  status, headers: { 'Content-Type': 'application/json' },
});
const progression = (state: 'available' | 'claimed' | 'unavailable') => ({
  achievements: [],
  daily: [{ id: 'check-in', title: 'Daily check-in', description: '', completed: state === 'claimed', reward: 'points' }],
  daily_status: { state, server_time: '2026-01-01T12:00:00+00:00', next_reset_at: '2026-01-02T00:00:00+00:00' },
});
const setup = (state: 'available' | 'claimed' | 'unavailable', last: string | null, failing = false) => {
  const daily = vi.fn();
  const fetcher = vi.fn((url: string) => Promise.resolve(url.includes('/progression')
    ? response(progression(state), failing ? 503 : 200)
    : response({ items: [], page: 1, limit: 3, total: 0 })));
  vi.stubGlobal('fetch', fetcher);
  render(<Home profile={{ ...profile, last_daily: last }} preview={false} revision={0}
    navigate={vi.fn()} openCharacter={vi.fn()} favorite={vi.fn()} daily={daily} dailyBusy={false} />);
  return { daily, fetcher };
};
afterEach(() => { cleanup(); vi.unstubAllGlobals(); });

it('allows a claim when the server says available even if the device date disagrees', async () => {
  const { daily } = setup('available', new Date().toISOString().slice(0, 10));
  const button = await screen.findByRole('button', { name: 'Claim daily reward' });
  expect(button).toBeEnabled();
  await userEvent.click(button);
  expect(daily).toHaveBeenCalledOnce();
});

it('disables a claim when the server says claimed regardless of the profile date', async () => {
  setup('claimed', '1999-01-01');
  expect(await screen.findByRole('button', { name: 'Claimed today' })).toBeDisabled();
});

it.each(['unavailable', 'error'] as const)('fails closed and offers refresh for %s', async state => {
  setup('unavailable', null, state === 'error');
  expect(await screen.findByRole('button', { name: 'Daily unavailable' })).toBeDisabled();
  expect(screen.getByRole('button', { name: 'Refresh daily status' })).toBeEnabled();
});

it('refreshes server status on returning to the visible app', async () => {
  const { fetcher } = setup('claimed', null);
  await screen.findByRole('button', { name: 'Claimed today' });
  const before = fetcher.mock.calls.filter(([url]) => url.includes('/progression')).length;
  fireEvent(document, new Event('visibilitychange'));
  await waitFor(() => expect(fetcher.mock.calls.filter(([url]) => url.includes('/progression')).length).toBeGreaterThan(before));
});

it('also fails closed in the progression screen', async () => {
  vi.stubGlobal('fetch', vi.fn(() => Promise.resolve(response(progression('unavailable')))));
  render(<Progress preview={false} revision={0} daily={vi.fn()} dailyBusy={false} />);
  expect(await screen.findByRole('button', { name: 'Unavailable' })).toBeDisabled();
});
