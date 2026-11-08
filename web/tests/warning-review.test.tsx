import { act, cleanup, render, screen, waitFor, within } from '@testing-library/react';
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
const id = 'a'.repeat(32);
const olderId = 'b'.repeat(32);
const warning = { id, actor_id: 42, reason: 'First warning', created_at: '2026-11-05T00:00:00Z' };
const older = { id: olderId, actor_id: 43, reason: 'Older warning', created_at: '2026-11-04T00:00:00Z' };
const reply = (data: unknown, status = 200) => new Response(JSON.stringify(status === 200 ? { data } : { error: { message: 'Unavailable' } }), { status, headers: { 'Content-Type': 'application/json' } });
const emptyPage = { items: [], active_count: 0, next_cursor: null };

afterEach(() => { cleanup(); vi.unstubAllGlobals(); });

it('requires explicit confirmation and sends the selected group, target and reason exactly once', async () => {
  let revoked = false;
  const fetcher = vi.fn((url: string, options: RequestInit) => {
    if (url === '/api/groups') return Promise.resolve(reply([group]));
    if (url === '/api/groups/-100') return Promise.resolve(reply(group));
    if (options.method === 'POST') { revoked = true; return Promise.resolve(reply({ id, status: 'revoked' })); }
    return Promise.resolve(reply(revoked ? emptyPage : { items: [warning], active_count: 1, next_cursor: null }));
  });
  vi.stubGlobal('fetch', fetcher);
  const user = userEvent.setup(); const notify = vi.fn();
  render(<Groups preview={false} revision={0} refresh={vi.fn()} notify={notify} />);
  await user.click(await screen.findByRole('tab', { name: 'Moderation' }));
  await user.type(screen.getByLabelText('Member ID to review'), '55');
  await user.click(screen.getByRole('button', { name: 'Find warnings' }));
  expect(await screen.findByText('First warning')).toBeInTheDocument();
  await user.click(screen.getByRole('button', { name: 'Revoke warning' }));
  let confirmation = screen.getByRole('region', { name: 'Confirm warning revocation' });
  expect(confirmation).toHaveFocus();
  expect(within(confirmation).getByText(/55/)).toBeInTheDocument();
  expect(fetcher.mock.calls.filter(([, options]) => options.method === 'POST')).toHaveLength(0);
  await user.click(within(confirmation).getByRole('button', { name: 'Cancel' }));
  expect(screen.getByRole('button', { name: 'Revoke warning' })).toHaveFocus();
  expect(fetcher.mock.calls.filter(([, options]) => options.method === 'POST')).toHaveLength(0);
  await user.click(screen.getByRole('button', { name: 'Revoke warning' }));
  confirmation = screen.getByRole('region', { name: 'Confirm warning revocation' });
  await user.type(within(confirmation).getByLabelText('Reason for revocation (optional)'), 'Recorded in error');
  await user.click(within(confirmation).getByRole('button', { name: 'Confirm revocation' }));
  await waitFor(() => expect(fetcher.mock.calls.filter(([, options]) => options.method === 'POST')).toHaveLength(1));
  const [url, options] = fetcher.mock.calls.find(([, opts]) => opts.method === 'POST')!;
  expect(url).toBe(`/api/groups/-100/members/55/warnings/${id}/revoke`);
  expect(JSON.parse(options.body as string)).toEqual({ reason: 'Recorded in error' });
  expect(new Headers(options.headers).get('X-Summon-Request')).toBe('1');
  expect(await screen.findByText('No active warnings for this member.')).toBeInTheDocument();
  expect(notify).toHaveBeenCalledWith('Warning revoked.');
});

it('offers bounded older pages and can return to the first page', async () => {
  const fetcher = vi.fn((url: string) => {
    if (url === '/api/groups') return Promise.resolve(reply([group]));
    if (url === '/api/groups/-100') return Promise.resolve(reply(group));
    if (url.includes(`before=${id}`)) return Promise.resolve(reply({ items: [older], active_count: 2, next_cursor: null }));
    return Promise.resolve(reply({ items: [warning], active_count: 2, next_cursor: id }));
  });
  vi.stubGlobal('fetch', fetcher);
  const user = userEvent.setup();
  render(<Groups preview={false} revision={0} refresh={vi.fn()} notify={vi.fn()} />);
  await user.click(await screen.findByRole('tab', { name: 'Moderation' }));
  await user.type(screen.getByLabelText('Member ID to review'), '55');
  await user.click(screen.getByRole('button', { name: 'Find warnings' }));
  expect(await screen.findByText('First warning')).toBeInTheDocument();
  await user.click(screen.getByRole('button', { name: 'Older warnings' }));
  expect(await screen.findByText('Older warning')).toBeInTheDocument();
  expect(screen.queryByText('First warning')).not.toBeInTheDocument();
  expect(fetcher.mock.calls.some(([url]) => url.includes(`/members/55/warnings?limit=5&before=${id}`))).toBe(true);
  await user.click(screen.getByRole('button', { name: 'First page' }));
  expect(await screen.findByText('First warning')).toBeInTheDocument();
});

it('invalidates an outstanding response when the target changes', async () => {
  let resolveOld!: (value: Response) => void;
  const old = new Promise<Response>(resolve => { resolveOld = resolve; });
  const fetcher = vi.fn((url: string) => {
    if (url === '/api/groups') return Promise.resolve(reply([group]));
    if (url === '/api/groups/-100') return Promise.resolve(reply(group));
    if (url.includes('/members/55/')) return old;
    return Promise.resolve(reply({ items: [{ ...older, reason: 'New target warning' }], active_count: 1, next_cursor: null }));
  });
  vi.stubGlobal('fetch', fetcher);
  const user = userEvent.setup();
  render(<Groups preview={false} revision={0} refresh={vi.fn()} notify={vi.fn()} />);
  await user.click(await screen.findByRole('tab', { name: 'Moderation' }));
  const input = screen.getByLabelText('Member ID to review');
  await user.type(input, '55'); await user.click(screen.getByRole('button', { name: 'Find warnings' }));
  await user.clear(input); await user.type(input, '56');
  await user.click(screen.getByRole('button', { name: 'Find warnings' }));
  expect(await screen.findByText('New target warning')).toBeInTheDocument();
  await act(async () => { resolveOld(reply({ items: [warning], active_count: 1, next_cursor: null })); });
  expect(screen.queryByText('First warning')).not.toBeInTheDocument();
  expect(screen.getByText('New target warning')).toBeInTheDocument();
});

it('does not retain old group results when switching groups during a request', async () => {
  let resolveOld!: (value: Response) => void;
  const old = new Promise<Response>(resolve => { resolveOld = resolve; });
  const second = { ...group, id: -101, title: 'Other Club' };
  const fetcher = vi.fn((url: string) => {
    if (url === '/api/groups') return Promise.resolve(reply([group, second]));
    if (url === '/api/groups/-100') return Promise.resolve(reply(group));
    if (url === '/api/groups/-101') return Promise.resolve(reply(second));
    if (url.includes('/groups/-100/members/')) return old;
    return Promise.resolve(reply(emptyPage));
  });
  vi.stubGlobal('fetch', fetcher);
  const user = userEvent.setup();
  render(<Groups preview={false} revision={0} refresh={vi.fn()} notify={vi.fn()} />);
  await user.click(await screen.findByRole('tab', { name: 'Moderation' }));
  await user.type(screen.getByLabelText('Member ID to review'), '55');
  await user.click(screen.getByRole('button', { name: 'Find warnings' }));
  await user.selectOptions(screen.getByLabelText('YOUR GROUP'), '-101');
  await screen.findByRole('heading', { name: 'Other Club' });
  await user.click(screen.getByRole('tab', { name: 'Moderation' }));
  await act(async () => { resolveOld(reply({ items: [warning], active_count: 1, next_cursor: null })); });
  expect(screen.queryByText('First warning')).not.toBeInTheDocument();
  expect(screen.getByLabelText('Member ID to review')).toHaveValue('');
});

it('does not automatically retry an uncertain mutation and clears the actionable result', async () => {
  const fetcher = vi.fn((url: string, options: RequestInit) => {
    if (url === '/api/groups') return Promise.resolve(reply([group]));
    if (url === '/api/groups/-100') return Promise.resolve(reply(group));
    if (options.method === 'POST') return Promise.resolve(reply(null, 503));
    return Promise.resolve(reply({ items: [warning], active_count: 1, next_cursor: null }));
  });
  vi.stubGlobal('fetch', fetcher);
  const user = userEvent.setup();
  render(<Groups preview={false} revision={0} refresh={vi.fn()} notify={vi.fn()} />);
  await user.click(await screen.findByRole('tab', { name: 'Moderation' }));
  await user.type(screen.getByLabelText('Member ID to review'), '55');
  await user.click(screen.getByRole('button', { name: 'Find warnings' }));
  await screen.findByText('First warning');
  await user.click(screen.getByRole('button', { name: 'Revoke warning' }));
  await user.click(screen.getByRole('button', { name: 'Confirm revocation' }));
  expect(await screen.findByText(/Review warnings before trying again/)).toBeInTheDocument();
  expect(screen.queryByRole('button', { name: 'Confirm revocation' })).not.toBeInTheDocument();
  expect(screen.queryByRole('button', { name: 'Revoke warning' })).not.toBeInTheDocument();
  expect(fetcher.mock.calls.filter(([, opts]) => opts.method === 'POST')).toHaveLength(1);
});

it('shows a clearly marked sample in preview but never enables revocation', async () => {
  const fetcher = vi.fn((url: string) => {
    if (url === '/api/groups') return Promise.resolve(reply([group]));
    if (url === '/api/groups/-100') return Promise.resolve(reply(group));
    return Promise.resolve(reply({ items: [{ ...warning, reason: 'Sample warning for review' }], active_count: 1, next_cursor: null }));
  });
  vi.stubGlobal('fetch', fetcher);
  const user = userEvent.setup();
  render(<Groups preview revision={0} refresh={vi.fn()} notify={vi.fn()} />);
  await user.click(await screen.findByRole('tab', { name: 'Moderation' }));
  expect(screen.getByLabelText('Member ID to review')).toHaveValue('456');
  await user.click(screen.getByRole('button', { name: 'Find warnings' }));
  expect(await screen.findByText('Sample warning for review')).toBeInTheDocument();
  expect(screen.getByRole('button', { name: 'Revoke warning' })).toBeDisabled();
});

it('hides moderation and warning review when the admin lacks the restriction right', async () => {
  const restricted = { ...group, permissions: { can_change_info: true, can_restrict_members: false } };
  vi.stubGlobal('fetch', vi.fn((url: string) => Promise.resolve(reply(url === '/api/groups' ? [restricted] : restricted))));
  render(<Groups preview={false} revision={0} refresh={vi.fn()} notify={vi.fn()} />);
  await screen.findByRole('tab', { name: 'Welcome & rules' });
  expect(screen.queryByRole('tab', { name: 'Moderation' })).not.toBeInTheDocument();
  expect(screen.queryByLabelText('Member ID to review')).not.toBeInTheDocument();
});

it('does not submit twice while a revocation is pending', async () => {
  let finish!: (value: Response) => void;
  const pending = new Promise<Response>(resolve => { finish = resolve; });
  const fetcher = vi.fn((url: string, options: RequestInit) => {
    if (url === '/api/groups') return Promise.resolve(reply([group]));
    if (url === '/api/groups/-100') return Promise.resolve(reply(group));
    if (options.method === 'POST') return pending;
    return Promise.resolve(reply({ items: [warning], active_count: 1, next_cursor: null }));
  });
  vi.stubGlobal('fetch', fetcher);
  const user = userEvent.setup();
  render(<Groups preview={false} revision={0} refresh={vi.fn()} notify={vi.fn()} />);
  await user.click(await screen.findByRole('tab', { name: 'Moderation' }));
  await user.type(screen.getByLabelText('Member ID to review'), '55');
  await user.click(screen.getByRole('button', { name: 'Find warnings' }));
  await screen.findByText('First warning');
  await user.click(screen.getByRole('button', { name: 'Revoke warning' }));
  await user.dblClick(screen.getByRole('button', { name: 'Confirm revocation' }));
  expect(fetcher.mock.calls.filter(([, options]) => options.method === 'POST')).toHaveLength(1);
  expect(screen.getByRole('button', { name: 'Confirming…' })).toBeDisabled();
  await act(async () => { finish(reply({ id, status: 'revoked' })); });
});

it('does not show an old group success notification after switching groups mid-mutation', async () => {
  let finish!: (value: Response) => void;
  const pending = new Promise<Response>(resolve => { finish = resolve; });
  const second = { ...group, id: -101, title: 'Other Club' };
  const fetcher = vi.fn((url: string, options: RequestInit) => {
    if (url === '/api/groups') return Promise.resolve(reply([group, second]));
    if (url === '/api/groups/-100') return Promise.resolve(reply(group));
    if (url === '/api/groups/-101') return Promise.resolve(reply(second));
    if (options.method === 'POST') return pending;
    return Promise.resolve(reply({ items: [warning], active_count: 1, next_cursor: null }));
  });
  vi.stubGlobal('fetch', fetcher);
  const user = userEvent.setup(); const notify = vi.fn();
  render(<Groups preview={false} revision={0} refresh={vi.fn()} notify={notify} />);
  await user.click(await screen.findByRole('tab', { name: 'Moderation' }));
  await user.type(screen.getByLabelText('Member ID to review'), '55');
  await user.click(screen.getByRole('button', { name: 'Find warnings' }));
  await screen.findByText('First warning');
  await user.click(screen.getByRole('button', { name: 'Revoke warning' }));
  await user.click(screen.getByRole('button', { name: 'Confirm revocation' }));
  await user.selectOptions(screen.getByLabelText('YOUR GROUP'), '-101');
  await screen.findByRole('heading', { name: 'Other Club' });
  await act(async () => { finish(reply({ id, status: 'revoked' })); });
  expect(notify).not.toHaveBeenCalledWith('Warning revoked.');
});

it('does not claim that Telegram applied a restriction when recording a warning', async () => {
  const fetcher = vi.fn((url: string, options: RequestInit) => {
    if (url === '/api/groups') return Promise.resolve(reply([group]));
    if (url === '/api/groups/-100') return Promise.resolve(reply(group));
    if (options.method === 'POST') return Promise.resolve(reply({ action: 'warn', warning_count: 1 }));
    return Promise.resolve(reply(emptyPage));
  });
  vi.stubGlobal('fetch', fetcher);
  const user = userEvent.setup(); const notify = vi.fn();
  render(<Groups preview={false} revision={0} refresh={vi.fn()} notify={notify} />);
  await user.click(await screen.findByRole('tab', { name: 'Moderation' }));
  await user.type(screen.getByLabelText('Telegram user ID'), '55');
  await user.click(screen.getByRole('button', { name: 'Apply action' }));
  await waitFor(() => expect(notify).toHaveBeenCalledWith('Warning recorded; no automatic penalty was applied.'));
});
