import '@testing-library/jest-dom/vitest';
import { cleanup, render, screen, within } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { afterEach, expect, it, vi } from 'vitest';
import Groups from '../src/pages/Groups';

const settings = { spawn_enabled: true, spawn_every: 80, spawn_cooldown_seconds: 300, spawn_lifetime_seconds: 300, welcome_enabled: false, welcome_text: 'Hi', goodbye_enabled: false, goodbye_text: 'Bye', rules: '', link_filter: false, flood_enabled: false, flood_limit: 6, flood_window_seconds: 10, duplicate_enabled: false, duplicate_limit: 3, mention_limit: 0, blocked_keywords: [], warning_limit: 3 };
const group = { id: -100, title: 'Test Club', total_messages: 10, settings, permissions: { can_change_info: true, can_restrict_members: true }, recent_activity: [] };
const report = { window_days: 30, as_of: '2026-11-02T12:00:00Z', attempts: 8, caught: 5, expired: 1, delivery_rejected: 1, awaiting_resolution: 1, my_catches: 2 };
const reply = (data: unknown, status = 200) => new Response(JSON.stringify(status < 400 ? { data } : { error: { message: String(data) } }), { status, headers: { 'Content-Type': 'application/json' } });
afterEach(() => { cleanup(); vi.unstubAllGlobals(); });

it('loads aggregate group insights only on demand and shows only the viewer’s count', async () => {
  const fetcher = vi.fn((url: string) => Promise.resolve(reply(url === '/api/groups' ? [group] : url.endsWith('/summon-insights') ? report : group)));
  vi.stubGlobal('fetch', fetcher);
  const user = userEvent.setup();
  render(<Groups preview revision={0} refresh={vi.fn()} notify={vi.fn()} />);
  const tab = await screen.findByRole('tab', { name: 'Summon insights' });
  expect(fetcher.mock.calls.some(([url]) => url.endsWith('/summon-insights'))).toBe(false);
  await user.click(tab);
  const section = await screen.findByRole('region', { name: 'Summon insights' });
  expect(within(section).getByText('Recorded attempts').nextElementSibling).toHaveTextContent('8');
  expect(within(section).getByText('Caught').nextElementSibling).toHaveTextContent('5');
  expect(within(section).getByText('Your catches here').nextElementSibling).toHaveTextContent('2');
  expect(section).toHaveTextContent(/may not have been delivered/i);
  expect(section).toHaveTextContent(/no other member identities/i);
  expect(fetcher.mock.calls.some(([url]) => url === '/api/groups/-100/summon-insights')).toBe(true);
  await user.click(within(section).getByRole('button', { name: 'Refresh insights' }));
  await screen.findByText('Recorded attempts');
  expect(fetcher.mock.calls.filter(([url]) => url.endsWith('/summon-insights'))).toHaveLength(2);
});

it('shows a recoverable error and retries the read without sending a mutation', async () => {
  let reads = 0;
  const fetcher = vi.fn((url: string, options: RequestInit) => {
    if (url.endsWith('/summon-insights')) return Promise.resolve(++reads === 1 ? reply('Unavailable', 503) : reply(report));
    return Promise.resolve(reply(url === '/api/groups' ? [group] : group));
  });
  vi.stubGlobal('fetch', fetcher);
  const user = userEvent.setup();
  render(<Groups preview={false} revision={0} refresh={vi.fn()} notify={vi.fn()} />);
  await user.click(await screen.findByRole('tab', { name: 'Summon insights' }));
  expect(await screen.findByRole('alert')).toHaveTextContent(/unavailable/i);
  await user.click(screen.getByRole('button', { name: 'Try again' }));
  expect(await screen.findByText('Recorded attempts')).toBeInTheDocument();
  expect(reads).toBe(2);
  expect(fetcher.mock.calls.every(([, options]) => options.method === 'GET')).toBe(true);
});

it('rejects inconsistent data rather than showing misleading counts', async () => {
  const fetcher = vi.fn((url: string) => Promise.resolve(reply(url === '/api/groups' ? [group] : url.endsWith('/summon-insights') ? { ...report, my_catches: 9 } : group)));
  vi.stubGlobal('fetch', fetcher);
  const user = userEvent.setup();
  render(<Groups preview revision={0} refresh={vi.fn()} notify={vi.fn()} />);
  await user.click(await screen.findByRole('tab', { name: 'Summon insights' }));
  expect(await screen.findByRole('alert')).toHaveTextContent(/unavailable/i);
  expect(screen.queryByText('Your catches here')).not.toBeInTheDocument();
});

it('does not show a late response after switching to another group', async () => {
  const other = { ...group, id: -200, title: 'Other Club' };
  const otherReport = { ...report, attempts: 3, caught: 1, expired: 1, delivery_rejected: 0, awaiting_resolution: 1, my_catches: 0 };
  let release: ((response: Response) => void) | undefined;
  const fetcher = vi.fn((url: string) => {
    if (url === '/api/groups') return Promise.resolve(reply([group, other]));
    if (url === '/api/groups/-100/summon-insights') return new Promise<Response>(resolve => { release = resolve; });
    if (url === '/api/groups/-200/summon-insights') return Promise.resolve(reply(otherReport));
    return Promise.resolve(reply(url.includes('-200') ? other : group));
  });
  vi.stubGlobal('fetch', fetcher);
  const user = userEvent.setup();
  render(<Groups preview revision={0} refresh={vi.fn()} notify={vi.fn()} />);
  await user.click(await screen.findByRole('tab', { name: 'Summon insights' }));
  expect(await screen.findByText(/Loading summon insights/i)).toBeInTheDocument();
  await user.selectOptions(screen.getByLabelText('YOUR GROUP'), '-200');
  await user.click(await screen.findByRole('tab', { name: 'Summon insights' }));
  const section = await screen.findByRole('region', { name: 'Summon insights' });
  expect(within(section).getByText('Recorded attempts').nextElementSibling).toHaveTextContent('3');
  release?.(reply(report));
  await Promise.resolve();
  expect(within(section).getByText('Recorded attempts').nextElementSibling).toHaveTextContent('3');
});
