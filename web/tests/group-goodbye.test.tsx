import { cleanup, render, screen, waitFor } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { afterEach, expect, it, vi } from 'vitest';
import Groups from '../src/pages/Groups';

const group = {
  id: -100, title: 'Test Club', total_messages: 42,
  settings: {
    spawn_enabled: false, spawn_every: 80, spawn_cooldown_seconds: 300, spawn_lifetime_seconds: 300,
    welcome_enabled: false, welcome_text: 'Welcome!', goodbye_enabled: false, goodbye_text: 'Take care!',
    rules: '', link_filter: false, flood_enabled: false, flood_limit: 6, flood_window_seconds: 10,
    duplicate_enabled: false, duplicate_limit: 3, mention_limit: 0, blocked_keywords: [], warning_limit: 3,
  },
  permissions: { can_change_info: true, can_restrict_members: true }, recent_activity: [],
};
const reply = (data: unknown) => new Response(JSON.stringify({ data }), { status: 200, headers: { 'Content-Type': 'application/json' } });
afterEach(() => { cleanup(); vi.unstubAllGlobals(); });

it('lets an admin save a real goodbye policy through the authenticated group API', async () => {
  const fetcher = vi.fn((url: string, options: RequestInit) => {
    if (options.method === 'PATCH') return Promise.resolve(reply(group));
    return Promise.resolve(reply(url === '/api/groups' ? [group] : group));
  });
  vi.stubGlobal('fetch', fetcher);
  const refresh = vi.fn(); const notify = vi.fn(); const user = userEvent.setup();
  render(<Groups preview={false} revision={0} refresh={refresh} notify={notify} />);
  await user.click(await screen.findByRole('tab', { name: 'Welcome & rules' }));
  await user.click(screen.getByRole('checkbox', { name: /Say goodbye to members/ }));
  const message = screen.getByLabelText('Goodbye message');
  await user.clear(message); await user.type(message, 'Thanks for being here!');
  await user.click(screen.getByRole('button', { name: 'Save community settings' }));
  await waitFor(() => expect(refresh).toHaveBeenCalledOnce());
  const request = fetcher.mock.calls.find(([url, options]) => url === '/api/groups/-100/settings' && options.method === 'PATCH');
  expect(request).toBeDefined();
  if (!request) throw new Error('Group request was not sent');
  expect(JSON.parse(request[1].body as string)).toMatchObject({ goodbye_enabled: true, goodbye_text: 'Thanks for being here!' });
  expect(new Headers(request[1].headers).get('X-Summon-Request')).toBe('1');
});

it('keeps goodbye controls and saving disabled in the sample preview', async () => {
  const fetcher = vi.fn((url: string) => Promise.resolve(reply(url === '/api/groups' ? [group] : group)));
  vi.stubGlobal('fetch', fetcher);
  const user = userEvent.setup();
  render(<Groups preview revision={0} refresh={vi.fn()} notify={vi.fn()} />);
  await user.click(await screen.findByRole('tab', { name: 'Welcome & rules' }));
  expect(screen.getByRole('checkbox', { name: /Say goodbye to members/ })).toBeDisabled();
  expect(screen.getByLabelText('Goodbye message')).toBeDisabled();
  expect(screen.getByRole('button', { name: 'Save community settings' })).toBeDisabled();
});

it('does not expose community controls to an admin without change-info permission', async () => {
  const restricted = { ...group, permissions: { can_change_info: false, can_restrict_members: true } };
  const fetcher = vi.fn((url: string) => Promise.resolve(reply(url === '/api/groups' ? [restricted] : restricted)));
  vi.stubGlobal('fetch', fetcher);
  render(<Groups preview={false} revision={0} refresh={vi.fn()} notify={vi.fn()} />);
  await screen.findByRole('tab', { name: 'Protection' });
  expect(screen.queryByRole('tab', { name: 'Welcome & rules' })).not.toBeInTheDocument();
});
