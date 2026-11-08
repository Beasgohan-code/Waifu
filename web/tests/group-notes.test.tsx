import { cleanup, fireEvent, render, screen, waitFor, within } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { afterEach, expect, it, vi } from 'vitest';
import App from '../src/App';
import Groups from '../src/pages/Groups';
import GroupNotes from '../src/pages/GroupNotes';

const rev = 'a'.repeat(32);
const updatedRev = 'b'.repeat(32);
const note = { name: 'welcome', content: 'Hello <friends>!', revision: rev, created_at: '2026-01-01T00:00:00Z', updated_at: '2026-01-01T00:00:00Z', created_by: 12, updated_by: 12 };
const group = {
  id: -100, title: 'Test Club', total_messages: 42,
  settings: { spawn_enabled: false, spawn_every: 80, spawn_cooldown_seconds: 300, spawn_lifetime_seconds: 300, welcome_enabled: false, welcome_text: 'Welcome!', goodbye_enabled: false, goodbye_text: 'Goodbye!', rules: '', link_filter: false, flood_enabled: false, flood_limit: 6, flood_window_seconds: 10, duplicate_enabled: false, duplicate_limit: 3, mention_limit: 0, blocked_keywords: [], warning_limit: 3 },
  permissions: { can_change_info: true, can_restrict_members: true }, recent_activity: [],
};
const reply = (data: unknown, status = 200) => new Response(JSON.stringify(status < 400 ? { data } : { error: { message: String(data) } }), { status, headers: { 'Content-Type': 'application/json' } });
afterEach(() => { cleanup(); vi.unstubAllGlobals(); });

it('exposes notes only to admins with change-info rights and loads them on demand', async () => {
  const fetcher = vi.fn((url: string) => Promise.resolve(reply(url === '/api/groups' ? [group] : url.endsWith('/notes') ? [note] : group)));
  vi.stubGlobal('fetch', fetcher);
  const user = userEvent.setup();
  const { unmount } = render(<Groups preview={false} revision={0} refresh={vi.fn()} notify={vi.fn()} />);
  const tab = await screen.findByRole('tab', { name: 'Notes' });
  expect(fetcher.mock.calls.some(([url]) => url.endsWith('/notes'))).toBe(false);
  await user.click(tab);
  expect(await screen.findByLabelText('Note text')).toHaveValue(note.content);
  expect(screen.getByText(/public in this group/i)).toBeInTheDocument();
  expect(document.querySelector('friends')).toBeNull();
  unmount();

  const restricted = { ...group, permissions: { can_change_info: false, can_restrict_members: true } };
  const deniedFetch = vi.fn((url: string) => Promise.resolve(reply(url === '/api/groups' ? [restricted] : restricted)));
  vi.stubGlobal('fetch', deniedFetch);
  render(<Groups preview={false} revision={0} refresh={vi.fn()} notify={vi.fn()} />);
  await screen.findByRole('tab', { name: 'Protection' });
  expect(screen.queryByRole('tab', { name: 'Notes' })).not.toBeInTheDocument();
  expect(deniedFetch.mock.calls.some(([url]) => url.endsWith('/notes'))).toBe(false);
});

it('saves a selected note with its original revision and the CSRF header', async () => {
  const fetcher = vi.fn((url: string, options: RequestInit) => Promise.resolve(reply(options.method === 'PUT' ? { ...note, content: 'Updated note', revision: updatedRev } : [note])));
  vi.stubGlobal('fetch', fetcher);
  const notify = vi.fn(); const user = userEvent.setup();
  render(<GroupNotes groupId={-100} preview={false} notify={notify} />);
  const text = await screen.findByLabelText('Note text');
  await user.clear(text); await user.type(text, 'Updated note');
  await user.click(screen.getByRole('button', { name: 'Save note' }));
  await waitFor(() => expect(notify).toHaveBeenCalledWith('Note saved.'));
  const request = fetcher.mock.calls.find(([url, options]) => url === '/api/groups/-100/notes/welcome' && options.method === 'PUT');
  expect(request).toBeDefined();
  if (!request) throw new Error('Note request not sent');
  expect(JSON.parse(request[1].body as string)).toEqual({ content: 'Updated note', expected_revision: rev });
  expect(new Headers(request[1].headers).get('X-Summon-Request')).toBe('1');
});

it('creates with an explicit null revision and prevents malformed or empty notes', async () => {
  const fetcher = vi.fn((url: string, options: RequestInit) => Promise.resolve(reply(options.method === 'PUT' ? { ...note, name: 'guide' } : [])));
  vi.stubGlobal('fetch', fetcher);
  const user = userEvent.setup();
  render(<GroupNotes groupId={-100} preview={false} notify={vi.fn()} />);
  const name = await screen.findByLabelText('Note name');
  const text = screen.getByLabelText('Note text');
  await user.type(name, 'bad.name'); await user.type(text, 'Hi');
  await user.click(screen.getByRole('button', { name: 'Save note' }));
  expect(screen.getByRole('alert')).toHaveTextContent(/lowercase letters/i);
  expect(fetcher.mock.calls.some(([, options]) => options.method === 'PUT')).toBe(false);
  await user.clear(name); await user.type(name, 'GUIDE');
  await user.click(screen.getByRole('button', { name: 'Save note' }));
  await waitFor(() => expect(fetcher.mock.calls.some(([url, options]) => url.endsWith('/notes/guide') && options.method === 'PUT')).toBe(true));
  const call = fetcher.mock.calls.find(([url, options]) => url.endsWith('/notes/guide') && options.method === 'PUT');
  expect(JSON.parse(call![1].body as string)).toEqual({ content: 'Hi', expected_revision: null });
});

it('keeps a conflicting draft and requires an explicit reload before retrying', async () => {
  let reads = 0;
  const fetcher = vi.fn((_url: string, options: RequestInit) => {
    if (options.method === 'PUT') return Promise.resolve(reply('Changed', 409));
    reads += 1; return Promise.resolve(reply([note]));
  });
  vi.stubGlobal('fetch', fetcher);
  const user = userEvent.setup();
  render(<GroupNotes groupId={-100} preview={false} notify={vi.fn()} />);
  const text = await screen.findByLabelText('Note text');
  await user.clear(text); await user.type(text, 'Keep my draft');
  await user.click(screen.getByRole('button', { name: 'Save note' }));
  expect(await screen.findByRole('alert')).toHaveTextContent(/copy your draft/i);
  expect(text).toHaveValue('Keep my draft');
  expect(text).not.toBeDisabled();
  expect(text).toHaveAttribute('readonly'); // The admin can still select and copy it.
  expect(screen.getByRole('button', { name: 'Save note' })).toBeDisabled();
  expect(reads).toBe(1);
  await user.click(screen.getByRole('button', { name: /Reload notes.*discard draft/i }));
  await waitFor(() => expect(reads).toBe(2));
  expect(await screen.findByLabelText('Note text')).toHaveValue(note.content);
});

it('confirms removal, restores cancel focus, and posts the displayed revision once', async () => {
  let release: ((value: Response) => void) | undefined;
  const fetcher = vi.fn((_url: string, options: RequestInit) => options.method === 'POST' ? new Promise<Response>(resolve => { release = resolve; }) : Promise.resolve(reply([note])));
  vi.stubGlobal('fetch', fetcher);
  const user = userEvent.setup(); const notify = vi.fn();
  render(<GroupNotes groupId={-100} preview={false} notify={notify} />);
  await screen.findByLabelText('Note text');
  const opener = screen.getByRole('button', { name: 'Delete note' });
  await user.click(opener);
  const region = screen.getByRole('region', { name: 'Confirm note deletion' });
  expect(region).toHaveFocus();
  expect(region).toHaveTextContent(/cannot be undone/i);
  expect(screen.getByLabelText('Note text')).toBeDisabled();
  expect(screen.getByRole('button', { name: 'Save note' })).toBeDisabled();
  expect(fetcher.mock.calls.some(([, options]) => options.method === 'POST')).toBe(false);
  await user.click(screen.getByRole('button', { name: 'Cancel' }));
  expect(opener).toHaveFocus();
  await user.click(opener);
  const confirm = screen.getByRole('button', { name: 'Confirm deletion' });
  fireEvent.click(confirm); fireEvent.click(confirm);
  expect(fetcher.mock.calls.filter(([, options]) => options.method === 'POST')).toHaveLength(1);
  const call = fetcher.mock.calls.find(([, options]) => options.method === 'POST');
  expect(call?.[0]).toBe('/api/groups/-100/notes/welcome/delete');
  expect(JSON.parse(call![1].body as string)).toEqual({ expected_revision: rev });
  if (!release) throw new Error('Delete request not pending');
  release(reply({ name: 'welcome', status: 'deleted' }));
  await waitFor(() => expect(notify).toHaveBeenCalledWith('Note deleted.'));
});

it('preserves draft and blocks mutation retries after an uncertain result', async () => {
  const fetcher = vi.fn((_url: string, options: RequestInit) => Promise.resolve(options.method === 'PUT' ? reply('unavailable', 503) : reply([note])));
  vi.stubGlobal('fetch', fetcher);
  const user = userEvent.setup();
  render(<GroupNotes groupId={-100} preview={false} notify={vi.fn()} />);
  const text = await screen.findByLabelText('Note text');
  await user.clear(text); await user.type(text, 'Still here');
  await user.click(screen.getByRole('button', { name: 'Save note' }));
  expect(await screen.findByRole('alert')).toHaveTextContent(/could not confirm/i);
  expect(text).toHaveValue('Still here');
  expect(screen.getByRole('button', { name: 'Save note' })).toBeDisabled();
  expect(screen.getByRole('button', { name: 'Delete note' })).toBeDisabled();
});

it('does not lose unsaved drafts when choosing another note', async () => {
  const another = { ...note, name: 'other', content: 'Different' };
  vi.stubGlobal('fetch', vi.fn(() => Promise.resolve(reply([note, another]))));
  const user = userEvent.setup();
  render(<GroupNotes groupId={-100} preview={false} notify={vi.fn()} />);
  const text = await screen.findByLabelText('Note text');
  await user.clear(text); await user.type(text, 'My draft');
  await user.click(screen.getByRole('button', { name: 'Edit note other' }));
  expect(screen.getByRole('alert')).toHaveTextContent(/save or discard/i);
  expect(text).toHaveValue('My draft');
  await user.click(screen.getByRole('button', { name: 'Discard draft' }));
  await user.click(screen.getByRole('button', { name: 'Edit note other' }));
  expect(screen.getByLabelText('Note text')).toHaveValue('Different');
});

it('renders synthetic preview notes but disables all writes', async () => {
  const fetcher = vi.fn(() => Promise.resolve(reply([note])));
  vi.stubGlobal('fetch', fetcher);
  render(<GroupNotes groupId={-100} preview notify={vi.fn()} />);
  expect(await screen.findByLabelText('Note text')).toBeDisabled();
  expect(screen.getByRole('button', { name: 'Save note' })).toBeDisabled();
  expect(screen.getByRole('button', { name: 'Delete note' })).toBeDisabled();
  expect(screen.getByRole('button', { name: 'New note' })).toBeDisabled();
  expect(screen.getByText(/sample data only/i)).toBeInTheDocument();
  expect(fetcher).toHaveBeenCalledTimes(1);
});

it('ignores a late read after changing groups', async () => {
  let release: ((value: Response) => void) | undefined;
  const fetcher = vi.fn((url: string) => url.includes('-100') ? new Promise<Response>(resolve => { release = resolve; }) : Promise.resolve(reply([{ ...note, name: 'new-group', content: 'From new group' }])));
  vi.stubGlobal('fetch', fetcher);
  const { rerender } = render(<GroupNotes groupId={-100} preview={false} notify={vi.fn()} />);
  rerender(<GroupNotes groupId={-200} preview={false} notify={vi.fn()} />);
  expect(await screen.findByLabelText('Note text')).toHaveValue('From new group');
  if (!release) throw new Error('First group request not pending');
  release(reply([note]));
  await waitFor(() => expect(screen.getByLabelText('Note text')).toHaveValue('From new group'));
  expect(screen.queryByRole('button', { name: 'Edit note welcome' })).not.toBeInTheDocument();
});

it('shows note audit activity without exposing note content', async () => {
  const withActivity = { ...group, recent_activity: [{ kind: 'note_changed', action: 'updated', note_name: 'guide', reason: null, created_at: '2026-01-01T00:00:00Z' }] };
  vi.stubGlobal('fetch', vi.fn((url: string) => Promise.resolve(reply(url === '/api/groups' ? [withActivity] : withActivity))));
  render(<Groups preview={false} revision={0} refresh={vi.fn()} notify={vi.fn()} />);
  expect(await screen.findByText('Note updated')).toBeInTheDocument();
  expect(screen.getByText('Note guide')).toBeInTheDocument();
});

it('ignores a late mutation from another group without blocking the new group', async () => {
  let release: ((value: Response) => void) | undefined;
  const other = { ...note, name: 'other', content: 'Other group' };
  const fetcher = vi.fn((url: string, options: RequestInit) => {
    if (options.method === 'PUT' && url.includes('-100')) return new Promise<Response>(resolve => { release = resolve; });
    if (options.method === 'PUT') return Promise.resolve(reply({ ...other, content: 'New group update' }));
    return Promise.resolve(reply(url.includes('-100') ? [note] : [other]));
  });
  vi.stubGlobal('fetch', fetcher);
  const notify = vi.fn(); const user = userEvent.setup();
  const { rerender } = render(<GroupNotes groupId={-100} preview={false} notify={notify} />);
  const first = await screen.findByLabelText('Note text');
  await user.clear(first); await user.type(first, 'First pending update');
  await user.click(screen.getByRole('button', { name: 'Save note' }));
  rerender(<GroupNotes groupId={-200} preview={false} notify={notify} />);
  const second = await screen.findByLabelText('Note text');
  await user.clear(second); await user.type(second, 'New group update');
  await user.click(screen.getByRole('button', { name: 'Save note' }));
  await waitFor(() => expect(fetcher.mock.calls.some(([url, options]) => url.includes('-200/notes/other') && options.method === 'PUT')).toBe(true));
  if (!release) throw new Error('First mutation not pending');
  release(reply({ ...note, content: 'First pending update' }));
  await waitFor(() => expect(notify).toHaveBeenCalledTimes(1));
});

it('asks before discarding a note draft when changing a group tab or group', async () => {
  const second = { ...group, id: -200, title: 'Second Club' };
  const fetcher = vi.fn((url: string) => Promise.resolve(reply(url === '/api/groups' ? [group, second] : url.endsWith('/notes') ? [note] : url.includes('-200') ? second : group)));
  vi.stubGlobal('fetch', fetcher);
  const user = userEvent.setup();
  render(<Groups preview={false} revision={0} refresh={vi.fn()} notify={vi.fn()} />);
  await user.click(await screen.findByRole('tab', { name: 'Notes' }));
  const text = await screen.findByLabelText('Note text');
  await user.clear(text); await user.type(text, 'Do not lose this');
  await user.click(screen.getByRole('tab', { name: 'Overview' }));
  let confirmation = screen.getByRole('region', { name: 'Discard note draft' });
  expect(confirmation).toHaveFocus();
  expect(screen.getByLabelText('Note text')).toHaveValue('Do not lose this');
  await user.click(screen.getByRole('button', { name: 'Keep editing' }));
  expect(screen.queryByRole('region', { name: 'Discard note draft' })).not.toBeInTheDocument();
  await user.selectOptions(screen.getByLabelText('YOUR GROUP'), '-200');
  confirmation = screen.getByRole('region', { name: 'Discard note draft' });
  expect(confirmation).toHaveFocus();
  expect(screen.getByLabelText('Note text')).toHaveValue('Do not lose this');
  await user.click(screen.getByRole('button', { name: 'Discard and continue' }));
  expect(await screen.findByRole('heading', { name: 'Second Club' })).toBeInTheDocument();
});


it('guards an unsaved note when navigating out of Groups and on browser unload', async () => {
  const profile = { id: 1, first_name: 'Mika', username: null, points: 0, xp: 0, level: 1, catch_count: 0, unique_characters: 0, catalog_size: 0, streak: 0, last_daily: null, is_operator: false };
  vi.stubGlobal('fetch', vi.fn((url: string) => Promise.resolve(reply(
    url === '/api/bootstrap' ? { preview: false } : url === '/api/me' ? profile :
    url === '/api/groups' ? [group] : url.endsWith('/notes') ? [note] :
    url.startsWith('/api/characters?') ? { items: [], total: 0, page: 1, limit: 24 } : group
  ))));
  vi.stubGlobal('scrollTo', vi.fn());
  window.history.replaceState(null, '', '#groups');
  const user = userEvent.setup();
  render(<App />);
  await user.click(await screen.findByRole('tab', { name: 'Notes' }));
  const text = await screen.findByLabelText('Note text');
  await user.clear(text); await user.type(text, 'Draft in progress');
  const unload = new Event('beforeunload', { cancelable: true });
  window.dispatchEvent(unload);
  expect(unload.defaultPrevented).toBe(true);
  const discover = within(screen.getByRole('navigation', { name: 'Main navigation' })).getByRole('button', { name: 'Discover' });
  await user.click(discover);
  expect(screen.getByRole('region', { name: 'Discard note draft' })).toHaveFocus();
  expect(screen.getByLabelText('Note text')).toHaveValue('Draft in progress');
  await user.click(screen.getByRole('button', { name: 'Keep editing' }));
  await user.click(discover);
  await user.click(screen.getByRole('button', { name: 'Discard and continue' }));
  expect(await screen.findByRole('heading', { name: 'Meet the characters.' })).toBeInTheDocument();
  window.history.replaceState(null, '', '/');
});

it('does not re-submit a confirmed save when the follow-up list refresh fails', async () => {
  let reads = 0;
  const fetcher = vi.fn((_url: string, options: RequestInit) => {
    if (options.method === 'PUT') return Promise.resolve(reply({ ...note, content: 'Saved but not refreshed', revision: updatedRev }));
    reads += 1;
    return Promise.resolve(reads === 1 ? reply([note]) : reply('List unavailable', 503));
  });
  vi.stubGlobal('fetch', fetcher);
  const user = userEvent.setup();
  render(<GroupNotes groupId={-100} preview={false} notify={vi.fn()} />);
  const text = await screen.findByLabelText('Note text');
  await user.clear(text); await user.type(text, 'Saved but not refreshed');
  await user.click(screen.getByRole('button', { name: 'Save note' }));
  expect(await screen.findByRole('alert')).toHaveTextContent(/saved.*could not.*reload/i);
  expect(text).toHaveValue('Saved but not refreshed');
  expect(text).toHaveAttribute('readonly');
  expect(screen.getByRole('button', { name: 'Save note' })).toBeDisabled();
  expect(screen.getByRole('button', { name: 'Delete note' })).toBeDisabled();
  expect(fetcher.mock.calls.filter(([, options]) => options.method === 'PUT')).toHaveLength(1);
});
