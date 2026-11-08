import { useLayoutEffect } from 'react';
import { act, cleanup, render, screen } from '@testing-library/react';
import { afterEach, expect, it, vi } from 'vitest';
import { useRemote } from '../src/hooks';

type Snapshot = { path: string | null; revision: number; data: string | null; loading: boolean; error: string };
const reply = (data: string) => new Response(JSON.stringify({ data }), { status: 200 });
const failure = (message: string) => new Response(JSON.stringify({ error: { message } }), { status: 403 });
function deferred<T>() {
  let resolve!: (value: T) => void;
  const promise = new Promise<T>(done => { resolve = done; });
  return { promise, resolve };
}
function Probe({ path, revision = 0, observe }: { path: string | null; revision?: number; observe: (value: Snapshot) => void }) {
  const state = useRemote<string>(path, revision);
  useLayoutEffect(() => { observe({ path, revision, ...state }); });
  return <div>{state.loading ? 'Loading' : state.error || state.data || 'Empty'}</div>;
}
afterEach(() => { cleanup(); vi.unstubAllGlobals(); });

it('masks old data immediately when the path or revision changes', async () => {
  const second = deferred<Response>();
  const third = deferred<Response>();
  const fetcher = vi.fn()
    .mockResolvedValueOnce(reply('Group A'))
    .mockReturnValueOnce(second.promise)
    .mockReturnValueOnce(third.promise);
  vi.stubGlobal('fetch', fetcher);
  const snapshots: Snapshot[] = [];
  const observe = (value: Snapshot) => { snapshots.push(value); };
  const view = render(<Probe path="/a" observe={observe} />);
  await screen.findByText('Group A');
  view.rerender(<Probe path="/b" observe={observe} />);
  expect(snapshots.filter(value => value.path === '/b').every(value => value.data === null && value.error === '' && value.loading)).toBe(true);
  expect(screen.getByText('Loading')).toBeInTheDocument();
  await act(async () => { second.resolve(reply('Group B')); });
  await screen.findByText('Group B');
  view.rerender(<Probe path="/b" revision={1} observe={observe} />);
  expect(snapshots.filter(value => value.revision === 1).every(value => value.data === null && value.error === '' && value.loading)).toBe(true);
  await act(async () => { third.resolve(reply('Group B updated')); });
  await screen.findByText('Group B updated');
  view.rerender(<Probe path={null} revision={1} observe={observe} />);
  expect(snapshots.filter(value => value.path === null).every(value => value.data === null && value.error === '' && !value.loading)).toBe(true);
});

it('does not retain an old error or allow an obsolete response to replace newer data', async () => {
  const old = deferred<Response>();
  const fetcher = vi.fn()
    .mockResolvedValueOnce(failure('Access denied to A'))
    .mockReturnValueOnce(old.promise)
    .mockResolvedValueOnce(reply('Group C'));
  vi.stubGlobal('fetch', fetcher);
  const snapshots: Snapshot[] = [];
  const observe = (value: Snapshot) => { snapshots.push(value); };
  const view = render(<Probe path="/a" observe={observe} />);
  await screen.findByText('Access denied to A');
  view.rerender(<Probe path="/b" observe={observe} />);
  expect(snapshots.filter(value => value.path === '/b').every(value => value.error === '' && value.data === null)).toBe(true);
  view.rerender(<Probe path="/c" observe={observe} />);
  await screen.findByText('Group C');
  await act(async () => { old.resolve(reply('Obsolete Group B')); });
  expect(screen.getByText('Group C')).toBeInTheDocument();
  expect(screen.queryByText('Obsolete Group B')).not.toBeInTheDocument();
});
