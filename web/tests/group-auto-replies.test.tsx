import { cleanup, fireEvent, render, screen, waitFor, within } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { afterEach, expect, it, vi } from 'vitest';
import { useState } from 'react';
import App from '../src/App';
import GroupAutoReplies from '../src/pages/GroupAutoReplies';
import Groups from '../src/pages/Groups';

const rev = 'a'.repeat(32);
const sample = { name: 'greeting', trigger: 'hello everyone', response: '<Welcome & enjoy!>', enabled: true, revision: rev,
  created_at: '2026-01-01T00:00:00Z', updated_at: '2026-01-01T00:00:00Z', created_by: 1, updated_by: 1 };
const group = {
  id: -100, title: 'Test Club', total_messages: 42,
  settings: { spawn_enabled: true, spawn_every: 60, spawn_cooldown_seconds: 60, spawn_lifetime_seconds: 600,
    welcome_enabled: false, welcome_text: '', goodbye_enabled: false, goodbye_text: '', rules: '',
    link_filter: false, flood_enabled: false, flood_limit: 5, flood_window_seconds: 10,
    duplicate_enabled: false, duplicate_limit: 3, mention_limit: 0, blocked_keywords: [], warning_limit: 3 },
  permissions: { can_change_info: true, can_restrict_members: true }, recent_activity: [],
};
const reply = (data: unknown, status = 200) => new Response(JSON.stringify(status < 400 ? { data } : { error: { message: String(data) } }), { status, headers: { 'Content-Type': 'application/json' } });
afterEach(() => { cleanup(); vi.unstubAllGlobals(); window.history.replaceState(null, '', '/'); });

it('loads rules on demand only for an administrator with change-info access', async () => {
  const fetcher = vi.fn((url: string) => Promise.resolve(reply(url === '/api/groups' ? [group] : url.endsWith('/auto-replies') ? [sample] : group)));
  vi.stubGlobal('fetch', fetcher);
  const user = userEvent.setup();
  const { unmount } = render(<Groups preview={false} revision={0} refresh={vi.fn()} notify={vi.fn()} />);
  const tab = await screen.findByRole('tab', { name: 'Auto replies' });
  expect(fetcher.mock.calls.some(([url]) => url.endsWith('/auto-replies'))).toBe(false);
  await user.click(tab);
  expect(await screen.findByLabelText('Reply text')).toHaveValue(sample.response);
  expect(screen.getByText(/public in this group/i)).toBeInTheDocument();
  expect(screen.getByText(/respond to an exact message/i)).toBeInTheDocument();
  unmount();

  const denied = { ...group, permissions: { can_change_info: false, can_restrict_members: true } };
  const fetchDenied = vi.fn((url: string) => Promise.resolve(reply(url === '/api/groups' ? [denied] : denied)));
  vi.stubGlobal('fetch', fetchDenied);
  render(<Groups preview={false} revision={0} refresh={vi.fn()} notify={vi.fn()} />);
  await screen.findByRole('tab', { name: 'Protection' });
  expect(screen.queryByRole('tab', { name: 'Auto replies' })).not.toBeInTheDocument();
  expect(fetchDenied.mock.calls.some(([url]) => url.endsWith('/auto-replies'))).toBe(false);
});

it('creates a disabled rule with explicit null revision and validates before sending', async () => {
  const fetcher = vi.fn((_url: string, options: RequestInit) => Promise.resolve(reply(options.method === 'PUT' ? sample : [])));
  vi.stubGlobal('fetch', fetcher);
  const user = userEvent.setup();
  render(<GroupAutoReplies groupId={-100} preview={false} notify={vi.fn()} />);
  const name = await screen.findByLabelText('Rule name');
  const trigger = screen.getByLabelText('Exact trigger');
  const text = screen.getByLabelText('Reply text');
  const enabled = screen.getByLabelText('Enable this auto reply');
  expect(enabled).not.toBeChecked();
  await user.type(name, 'bad.name'); await user.type(trigger, 'hello'); await user.type(text, 'Welcome');
  await user.click(screen.getByRole('button', { name: 'Save auto reply' }));
  expect(screen.getByRole('alert')).toHaveTextContent(/lowercase letters/i);
  expect(fetcher.mock.calls.some(([, options]) => options.method === 'PUT')).toBe(false);
  await user.clear(name); await user.type(name, 'GREET');
  await user.clear(trigger); await user.type(trigger, '/help');
  await user.click(screen.getByRole('button', { name: 'Save auto reply' }));
  expect(screen.getByRole('alert')).toHaveTextContent(/trigger/i);
  await user.clear(trigger); await user.type(trigger, 'Hello everyone');
  await user.click(screen.getByRole('button', { name: 'Save auto reply' }));
  await waitFor(() => expect(fetcher.mock.calls.some(([url, options]) => url.endsWith('/auto-replies/greet') && options.method === 'PUT')).toBe(true));
  const call = fetcher.mock.calls.find(([url, options]) => url.endsWith('/auto-replies/greet') && options.method === 'PUT');
  expect(JSON.parse(call![1].body as string)).toEqual({ trigger: 'Hello everyone', response: 'Welcome', enabled: false, expected_revision: null });
  expect(new Headers(call![1].headers).get('X-Summon-Request')).toBe('1');
});

it('saves the displayed revision and requires reload after a conflict or uncertain result', async () => {
  let reads = 0;
  const fetcher = vi.fn((_url: string, options: RequestInit) => {
    if (options.method === 'PUT') return Promise.resolve(reply('conflict', 409));
    reads += 1; return Promise.resolve(reply([sample]));
  });
  vi.stubGlobal('fetch', fetcher);
  const user = userEvent.setup();
  render(<GroupAutoReplies groupId={-100} preview={false} notify={vi.fn()} />);
  const text = await screen.findByLabelText('Reply text');
  await user.clear(text); await user.type(text, 'Keep my draft');
  await user.click(screen.getByLabelText('Enable this auto reply'));
  await user.click(screen.getByRole('button', { name: 'Save auto reply' }));
  expect(await screen.findByRole('alert')).toHaveTextContent(/copy your draft/i);
  expect(text).toHaveValue('Keep my draft');
  expect(text).toHaveAttribute('readonly');
  expect(screen.getByRole('button', { name: 'Save auto reply' })).toBeDisabled();
  const call = fetcher.mock.calls.find(([, options]) => options.method === 'PUT');
  expect(JSON.parse(call![1].body as string)).toEqual({ trigger: sample.trigger, response: 'Keep my draft', enabled: false, expected_revision: rev });
  expect(reads).toBe(1);
  await user.click(screen.getByRole('button', { name: /Reload auto replies.*discard draft/i }));
  await waitFor(() => expect(reads).toBe(2));
  expect(await screen.findByLabelText('Reply text')).toHaveValue(sample.response);
});

it('allows correction of a definitively invalid response without discarding the draft', async () => {
  const fetcher = vi.fn((_url: string, options: RequestInit) => Promise.resolve(options.method === 'PUT' ? reply('invalid', 422) : reply([sample])));
  vi.stubGlobal('fetch', fetcher);
  const user = userEvent.setup();
  render(<GroupAutoReplies groupId={-100} preview={false} notify={vi.fn()} />);
  const text = await screen.findByLabelText('Reply text');
  await user.clear(text); await user.type(text, 'Draft');
  await user.click(screen.getByRole('button', { name: 'Save auto reply' }));
  expect(await screen.findByRole('alert')).toHaveTextContent(/check.*trigger.*reply/i);
  expect(text).not.toHaveAttribute('readonly');
  expect(text).toHaveValue('Draft');
  expect(screen.getByRole('button', { name: 'Save auto reply' })).toBeEnabled();
});

it('requires focused deletion confirmation, restores focus and sends once with revision', async () => {
  let release: ((value: Response) => void) | undefined;
  const fetcher = vi.fn((_url: string, options: RequestInit) => options.method === 'POST' ? new Promise<Response>(resolve => { release = resolve; }) : Promise.resolve(reply([sample])));
  vi.stubGlobal('fetch', fetcher);
  const user = userEvent.setup();
  render(<GroupAutoReplies groupId={-100} preview={false} notify={vi.fn()} />);
  await screen.findByLabelText('Reply text');
  const opener = screen.getByRole('button', { name: 'Delete auto reply' });
  await user.click(opener);
  const region = screen.getByRole('region', { name: 'Confirm auto-reply deletion' });
  expect(region).toHaveFocus();
  expect(region).toHaveTextContent(/cannot be undone/i);
  await user.click(within(region).getByRole('button', { name: 'Cancel' }));
  expect(opener).toHaveFocus();
  await user.click(opener);
  const confirm = screen.getByRole('button', { name: 'Confirm deletion' });
  fireEvent.click(confirm); fireEvent.click(confirm);
  const calls = fetcher.mock.calls.filter(([, options]) => options.method === 'POST');
  expect(calls).toHaveLength(1);
  expect(calls[0][0]).toBe('/api/groups/-100/auto-replies/greeting/delete');
  expect(JSON.parse(calls[0][1].body as string)).toEqual({ expected_revision: rev });
  if (!release) throw new Error('Delete request not pending');
  release(reply({ name: 'greeting', status: 'deleted' }));
});

it('guards drafts when switching rules, tabs, groups, application pages and browser unload', async () => {
  const other = { ...sample, name: 'other', trigger: 'another trigger', response: 'Other rule' };
  const second = { ...group, id: -200, title: 'Second Club' };
  const profile = { id: 1, first_name: 'Mika', username: null, points: 0, xp: 0, level: 1, catch_count: 0, unique_characters: 0, catalog_size: 0, streak: 0, last_daily: null, is_operator: false };
  vi.stubGlobal('fetch', vi.fn((url: string) => Promise.resolve(reply(
    url === '/api/bootstrap' ? { preview: false } : url === '/api/me' ? profile :
    url === '/api/groups' ? [group, second] : url.endsWith('/auto-replies') ? [sample, other] :
    url.startsWith('/api/characters?') ? { items: [], total: 0, page: 1, limit: 24 } : url.includes('-200') ? second : group
  ))));
  vi.stubGlobal('scrollTo', vi.fn());
  window.history.replaceState(null, '', '#groups');
  const user = userEvent.setup();
  render(<App />);
  await user.click(await screen.findByRole('tab', { name: 'Auto replies' }));
  const text = await screen.findByLabelText('Reply text');
  await user.clear(text); await user.type(text, 'Do not lose');
  await user.click(screen.getByRole('button', { name: 'Edit auto reply other' }));
  expect(screen.getByRole('alert')).toHaveTextContent(/save or discard/i);
  expect(text).toHaveValue('Do not lose');
  await user.click(screen.getByRole('tab', { name: 'Overview' }));
  let region = screen.getByRole('region', { name: 'Discard auto-reply draft' });
  expect(region).toHaveFocus();
  await user.click(within(region).getByRole('button', { name: 'Keep editing' }));
  await user.selectOptions(screen.getByLabelText('YOUR GROUP'), '-200');
  region = screen.getByRole('region', { name: 'Discard auto-reply draft' });
  await user.click(within(region).getByRole('button', { name: 'Keep editing' }));
  const unload = new Event('beforeunload', { cancelable: true });
  window.dispatchEvent(unload); expect(unload.defaultPrevented).toBe(true);
  const discover = within(screen.getByRole('navigation', { name: 'Main navigation' })).getByRole('button', { name: 'Discover' });
  await user.click(discover);
  region = screen.getByRole('region', { name: 'Discard auto-reply draft' });
  expect(region).toHaveFocus();
  await user.click(within(region).getByRole('button', { name: 'Discard and continue' }));
  expect(await screen.findByRole('heading', { name: 'Meet the characters.' })).toBeInTheDocument();
});

it('shows only synthetic read-only rules in preview and counts Unicode code points', async () => {
  const fetcher = vi.fn(() => Promise.resolve(reply([{ ...sample, response: '😀'.repeat(800) }])));
  vi.stubGlobal('fetch', fetcher);
  render(<GroupAutoReplies groupId={-100} preview notify={vi.fn()} />);
  expect(await screen.findByLabelText('Reply text')).toBeDisabled();
  expect(screen.getByText(/800 \/ 800 characters/)).toBeInTheDocument();
  expect(screen.getByRole('button', { name: 'Save auto reply' })).toBeDisabled();
  expect(screen.getByRole('button', { name: 'Delete auto reply' })).toBeDisabled();
  expect(screen.getByRole('button', { name: 'New auto reply' })).toBeDisabled();
  expect(screen.getByText(/sample data only/i)).toBeInTheDocument();
  expect(fetcher).toHaveBeenCalledTimes(1);
});

it('ignores a late read for another group', async () => {
  let release: ((value: Response) => void) | undefined;
  const fetcher = vi.fn((url: string) => url.includes('-100') ? new Promise<Response>(resolve => { release = resolve; }) : Promise.resolve(reply([{ ...sample, name: 'new-group', response: 'New group' }])));
  vi.stubGlobal('fetch', fetcher);
  const { rerender } = render(<GroupAutoReplies groupId={-100} preview={false} notify={vi.fn()} />);
  rerender(<GroupAutoReplies groupId={-200} preview={false} notify={vi.fn()} />);
  expect(await screen.findByLabelText('Reply text')).toHaveValue('New group');
  if (!release) throw new Error('First read not pending');
  release(reply([sample]));
  await waitFor(() => expect(screen.getByLabelText('Reply text')).toHaveValue('New group'));
  expect(screen.queryByRole('button', { name: 'Edit auto reply greeting' })).not.toBeInTheDocument();
});

it('keeps a confirmed save draft visible if its follow-up list reload fails inside Groups', async () => {
  let reads = 0;
  let groupReads = 0;
  const fetcher = vi.fn((url: string, options: RequestInit) => {
    if (url === '/api/groups') return Promise.resolve(reply([group]));
    if (url.endsWith('/auto-replies')) {
      reads += 1;
      return Promise.resolve(reads === 1 ? reply([sample]) : reply('unavailable', 503));
    }
    if (options.method === 'PUT') return Promise.resolve(reply({ ...sample, response: 'Saved draft' }));
    groupReads += 1;
    // An unrelated group refresh can be slow; it must not unmount the failed reload.
    return groupReads === 1 ? Promise.resolve(reply(group)) : new Promise<Response>(() => {});
  });
  vi.stubGlobal('fetch', fetcher);
  const user = userEvent.setup();
  function Harness() {
    const [revision, setRevision] = useState(0);
    return <Groups preview={false} revision={revision} refresh={() => setRevision(value => value + 1)} notify={vi.fn()} />;
  }
  render(<Harness />);
  await user.click(await screen.findByRole('tab', { name: 'Auto replies' }));
  const text = await screen.findByLabelText('Reply text');
  await user.clear(text); await user.type(text, 'Saved draft');
  await user.click(screen.getByRole('button', { name: 'Save auto reply' }));
  expect(await screen.findByRole('alert')).toHaveTextContent(/saved.*could not reload/i);
  expect(text).toHaveValue('Saved draft');
  expect(text).toHaveAttribute('readonly');
  expect(screen.getByRole('button', { name: 'Save auto reply' })).toBeDisabled();
  expect(fetcher.mock.calls.filter(([, options]) => options.method === 'PUT')).toHaveLength(1);
});

it('does not resubmit after an uncertain save', async () => {
  const fetcher = vi.fn((_url: string, options: RequestInit) => Promise.resolve(options.method === 'PUT' ? reply('unavailable', 503) : reply([sample])));
  vi.stubGlobal('fetch', fetcher);
  const user = userEvent.setup();
  render(<GroupAutoReplies groupId={-100} preview={false} notify={vi.fn()} />);
  const text = await screen.findByLabelText('Reply text');
  await user.clear(text); await user.type(text, 'Still here');
  await user.click(screen.getByRole('button', { name: 'Save auto reply' }));
  expect(await screen.findByRole('alert')).toHaveTextContent(/could not confirm/i);
  expect(text).toHaveValue('Still here');
  expect(screen.getByRole('button', { name: 'Save auto reply' })).toBeDisabled();
  expect(screen.getByRole('button', { name: 'Delete auto reply' })).toBeDisabled();
  expect(fetcher.mock.calls.filter(([, options]) => options.method === 'PUT')).toHaveLength(1);
});

it('enforces the 20-rule cap in the editor while allowing an existing rule to be edited', async () => {
  const rules = Array.from({ length: 20 }, (_, index) => ({ ...sample, name: `rule-${index}`, trigger: `trigger ${index}` }));
  vi.stubGlobal('fetch', vi.fn(() => Promise.resolve(reply(rules))));
  render(<GroupAutoReplies groupId={-100} preview={false} notify={vi.fn()} />);
  expect(await screen.findByLabelText('Reply text')).toHaveValue(sample.response);
  expect(screen.getByRole('button', { name: 'New auto reply' })).toBeDisabled();
  expect(screen.getByRole('button', { name: 'Save auto reply' })).toBeEnabled();
  expect(screen.getByText(/20-rule limit/i)).toBeInTheDocument();
});

it('ignores a late mutation from another group and keeps the new group usable', async () => {
  let release: ((value: Response) => void) | undefined;
  const other = { ...sample, name: 'other', response: 'Other group' };
  const fetcher = vi.fn((url: string, options: RequestInit) => {
    if (options.method === 'PUT' && url.includes('-100')) return new Promise<Response>(resolve => { release = resolve; });
    if (options.method === 'PUT') return Promise.resolve(reply({ ...other, response: 'New group update' }));
    return Promise.resolve(reply(url.includes('-100') ? [sample] : [other]));
  });
  vi.stubGlobal('fetch', fetcher);
  const notify = vi.fn(); const user = userEvent.setup();
  const { rerender } = render(<GroupAutoReplies groupId={-100} preview={false} notify={notify} />);
  const first = await screen.findByLabelText('Reply text');
  await user.clear(first); await user.type(first, 'First pending update');
  await user.click(screen.getByRole('button', { name: 'Save auto reply' }));
  rerender(<GroupAutoReplies groupId={-200} preview={false} notify={notify} />);
  const second = await screen.findByLabelText('Reply text');
  await user.clear(second); await user.type(second, 'New group update');
  await user.click(screen.getByRole('button', { name: 'Save auto reply' }));
  await waitFor(() => expect(fetcher.mock.calls.some(([url, options]) => url.includes('-200/auto-replies/other') && options.method === 'PUT')).toBe(true));
  if (!release) throw new Error('First mutation not pending');
  release(reply({ ...sample, response: 'First pending update' }));
  await waitFor(() => expect(notify).toHaveBeenCalledTimes(1));
});
