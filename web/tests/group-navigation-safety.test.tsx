import { act, cleanup, fireEvent, render, screen, waitFor, within } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { afterEach, expect, it, vi } from 'vitest';
import App from '../src/App';
import Groups from '../src/pages/Groups';

const groupA = {
  id: -100, title: 'Club A', total_messages: 10,
  settings: { spawn_enabled: true, spawn_every: 80, spawn_cooldown_seconds: 300, spawn_lifetime_seconds: 300,
    welcome_enabled: false, welcome_text: 'A', goodbye_enabled: false, goodbye_text: 'Bye A', rules: '',
    link_filter: false, flood_enabled: false, flood_limit: 6, flood_window_seconds: 10, duplicate_enabled: false,
    duplicate_limit: 3, mention_limit: 0, blocked_keywords: [], warning_limit: 3 },
  permissions: { can_change_info: true, can_restrict_members: true }, recent_activity: [],
};
const groupB = { ...groupA, id: -200, title: 'Club B', settings: { ...groupA.settings, spawn_every: 160 } };
const reply = (data: unknown) => new Response(JSON.stringify({ data }), { status: 200 });
function deferred<T>() {
  let resolve!: (value: T) => void;
  const promise = new Promise<T>(done => { resolve = done; });
  return { promise, resolve };
}
afterEach(() => { cleanup(); vi.unstubAllGlobals(); window.history.replaceState(null, '', '/'); });

it('hides the old group and cannot save its settings while switching to a different group', async () => {
  const groupBRead = deferred<Response>();
  const fetcher = vi.fn((url: string, options: RequestInit) => {
    if (options.method === 'PATCH') return Promise.resolve(reply(groupB));
    if (url === '/api/groups') return Promise.resolve(reply([groupA, groupB]));
    if (url === '/api/groups/-100') return Promise.resolve(reply(groupA));
    if (url === '/api/groups/-200') return groupBRead.promise;
    throw new Error(`Unexpected request: ${url}`);
  });
  vi.stubGlobal('fetch', fetcher);
  render(<Groups preview={false} revision={0} refresh={vi.fn()} notify={vi.fn()} />);
  const save = await screen.findByRole('button', { name: 'Save summon settings' });
  const select = screen.getByLabelText('YOUR GROUP');
  // Model a selection and a stale queued click before React has committed the new view.
  act(() => { fireEvent.change(select, { target: { value: '-200' } }); fireEvent.click(save); });
  expect(fetcher.mock.calls.filter(([, options]) => options.method === 'PATCH')).toHaveLength(0);
  expect(screen.queryByRole('heading', { name: 'Club A' })).not.toBeInTheDocument();
  expect(screen.getByText('Loading group…')).toBeInTheDocument();
  await act(async () => { groupBRead.resolve(reply(groupB)); });
  expect(await screen.findByRole('heading', { name: 'Club B' })).toBeInTheDocument();
  expect(screen.getByLabelText('Messages per summon')).toHaveValue(160);
  await userEvent.click(screen.getByRole('button', { name: 'Save summon settings' }));
  await waitFor(() => expect(fetcher.mock.calls.filter(([, options]) => options.method === 'PATCH')).toHaveLength(1));
  const patch = fetcher.mock.calls.find(([, options]) => options.method === 'PATCH');
  expect(patch?.[0]).toBe('/api/groups/-200/settings');
  expect(JSON.parse(patch?.[1].body as string).spawn_every).toBe(160);
});

it('locks rapid moderation submissions and prevents group or tab changes until the write resolves', async () => {
  const pending = deferred<Response>();
  const fetcher = vi.fn((url: string, options: RequestInit) => {
    if (options.method === 'POST') return pending.promise;
    if (url === '/api/groups') return Promise.resolve(reply([groupA, groupB]));
    if (url === '/api/groups/-100') return Promise.resolve(reply(groupA));
    if (url === '/api/groups/-200') return Promise.resolve(reply(groupB));
    throw new Error(`Unexpected request: ${url}`);
  });
  vi.stubGlobal('fetch', fetcher);
  const notify = vi.fn(); const user = userEvent.setup();
  render(<Groups preview={false} revision={0} refresh={vi.fn()} notify={notify} />);
  await user.click(await screen.findByRole('tab', { name: 'Moderation' }));
  await user.type(screen.getByLabelText('Telegram user ID'), '456');
  const form = screen.getByRole('button', { name: 'Apply action' }).closest('form');
  if (!form) throw new Error('Moderation form not found');
  const select = screen.getByLabelText('YOUR GROUP');
  act(() => {
    fireEvent.submit(form); fireEvent.submit(form);
    fireEvent.change(select, { target: { value: '-200' } });
    fireEvent.click(screen.getByRole('tab', { name: 'Overview' }));
  });
  expect(fetcher.mock.calls.filter(([, options]) => options.method === 'POST')).toHaveLength(1);
  expect(fetcher.mock.calls.some(([url]) => url === '/api/groups/-200')).toBe(false);
  expect(select).toBeDisabled();
  expect(screen.getByRole('tab', { name: 'Overview' })).toBeDisabled();
  expect(screen.getByLabelText('Telegram user ID')).toBeDisabled();
  await act(async () => { pending.resolve(reply({ action: 'warn', warning_count: 1 })); });
  await waitFor(() => expect(select).not.toBeDisabled());
  expect(notify).toHaveBeenCalledWith('Warning recorded; no automatic penalty was applied.');
  expect(fetcher.mock.calls.find(([, options]) => options.method === 'POST')?.[0]).toBe('/api/groups/-100/moderation');
});

it('warns that an unconfirmed moderation write may have completed instead of retrying it', async () => {
  const fetcher = vi.fn((url: string, options: RequestInit) => {
    if (options.method === 'POST') return Promise.reject(new TypeError('Network unavailable'));
    return Promise.resolve(reply(url === '/api/groups' ? [groupA] : groupA));
  });
  vi.stubGlobal('fetch', fetcher);
  const notify = vi.fn(); const user = userEvent.setup();
  render(<Groups preview={false} revision={0} refresh={vi.fn()} notify={notify} />);
  await user.click(await screen.findByRole('tab', { name: 'Moderation' }));
  await user.type(screen.getByLabelText('Telegram user ID'), '456');
  await user.click(screen.getByRole('button', { name: 'Apply action' }));
  await waitFor(() => expect(notify).toHaveBeenCalledWith(expect.stringMatching(/may have completed.*before retrying/i), true));
  const apply = screen.getByRole('button', { name: 'Apply action' });
  expect(apply).toBeDisabled();
  expect(screen.getByRole('alert')).toHaveTextContent('member 456 in group -100');
  const form = apply.closest('form');
  if (!form) throw new Error('Moderation form not found');
  fireEvent.submit(form);
  expect(fetcher.mock.calls.filter(([, options]) => options.method === 'POST')).toHaveLength(1);
  await user.click(screen.getByRole('button', { name: "I've reviewed the action" }));
  expect(apply).toBeEnabled();
});

it('shows a definitive permission rejection without claiming an uncertain action', async () => {
  const fetcher = vi.fn((url: string, options: RequestInit) => {
    if (options.method === 'POST') return Promise.resolve(new Response(JSON.stringify({ error: { message: 'Permission denied' } }), { status: 403 }));
    return Promise.resolve(reply(url === '/api/groups' ? [groupA] : groupA));
  });
  vi.stubGlobal('fetch', fetcher);
  const notify = vi.fn(); const user = userEvent.setup();
  render(<Groups preview={false} revision={0} refresh={vi.fn()} notify={notify} />);
  await user.click(await screen.findByRole('tab', { name: 'Moderation' }));
  await user.type(screen.getByLabelText('Telegram user ID'), '456');
  await user.click(screen.getByRole('button', { name: 'Apply action' }));
  await waitFor(() => expect(notify).toHaveBeenCalledWith('Permission denied', true));
  expect(screen.queryByRole('alert')).not.toBeInTheDocument();
  expect(screen.getByRole('button', { name: 'Apply action' })).toBeEnabled();
});

it('guards app navigation and browser unload during a group mutation', async () => {
  const pending = deferred<Response>();
  const profile = { id: 1, first_name: 'Mika', username: null, points: 0, xp: 0, level: 1, catch_count: 0, unique_characters: 0, catalog_size: 0, streak: 0, last_daily: null, is_operator: false };
  const fetcher = vi.fn((url: string, options: RequestInit) => {
    if (options.method === 'PATCH') return pending.promise;
    if (url === '/api/bootstrap') return Promise.resolve(reply({ preview: false }));
    if (url === '/api/me') return Promise.resolve(reply(profile));
    if (url === '/api/groups') return Promise.resolve(reply([groupA]));
    if (url === '/api/groups/-100') return Promise.resolve(reply(groupA));
    if (url.startsWith('/api/characters?')) return Promise.resolve(reply({ items: [], total: 0, page: 1, limit: 24 }));
    throw new Error(`Unexpected request: ${url}`);
  });
  vi.stubGlobal('fetch', fetcher); vi.stubGlobal('scrollTo', vi.fn());
  window.history.replaceState(null, '', '#groups');
  const user = userEvent.setup();
  render(<App />);
  await user.click(await screen.findByRole('button', { name: 'Save summon settings' }));
  expect(screen.getByText(/A group change is in progress/)).toBeInTheDocument();
  const unload = new Event('beforeunload', { cancelable: true });
  window.dispatchEvent(unload);
  expect(unload.defaultPrevented).toBe(true);
  const discover = within(screen.getByRole('navigation', { name: 'Main navigation' })).getByRole('button', { name: 'Discover' });
  await user.click(discover);
  expect(screen.getByRole('heading', { name: 'Club A' })).toBeInTheDocument();
  expect(screen.getByText(/wait for the group change to finish/i)).toBeInTheDocument();
  await act(async () => { pending.resolve(reply(groupA)); });
  await screen.findByRole('button', { name: 'Save summon settings' });
  await user.click(discover);
  expect(await screen.findByRole('heading', { name: 'Meet the characters.' })).toBeInTheDocument();
  expect(fetcher.mock.calls.filter(([, options]) => options.method === 'PATCH')).toHaveLength(1);
});
