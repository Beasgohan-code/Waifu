import { cleanup, render, screen, waitFor } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { afterEach, expect, it, vi } from 'vitest';
import Catalog from '../src/pages/Catalog';

const first = { id: 'aiko', name: 'Aiko', category: 'Moon', rarity: 'rare', image_url: '/art/aiko.webp', description: '', quantity: 1, favorite: true, wishlisted: false, active: true };
const duplicate = { ...first, id: 'ren', name: 'Ren', quantity: 2, favorite: false };
const reply = (data: unknown) => new Response(JSON.stringify({ data }), { status: 200, headers: { 'Content-Type': 'application/json' } });
const props = { mode: 'collection' as const, preview: false, revision: 0, onOpen: vi.fn(), onFavorite: vi.fn(), onDiscover: vi.fn() };
afterEach(() => { cleanup(); vi.unstubAllGlobals(); });

it('requests duplicate-only results and copy sorting from the server', async () => {
  const fetcher = vi.fn((url: string) => {
    const params = new URL(url, 'https://miniapp.example').searchParams;
    const items = params.get('duplicates') === 'true' ? [duplicate] : [first, duplicate];
    return Promise.resolve(reply({ items, total: items.length, page: 1, limit: 24, page_cursor: 'first', next_cursor: null }));
  });
  vi.stubGlobal('fetch', fetcher);
  const user = userEvent.setup();
  render(<Catalog {...props} />);
  expect(await screen.findByRole('button', { name: 'View Aiko' })).toBeInTheDocument();
  const duplicates = screen.getByRole('button', { name: 'Duplicates' });
  await user.click(duplicates);
  expect(duplicates).toHaveAttribute('aria-pressed', 'true');
  expect(await screen.findByRole('button', { name: 'View Ren' })).toBeInTheDocument();
  await waitFor(() => expect(screen.queryByRole('button', { name: 'View Aiko' })).not.toBeInTheDocument());
  await user.selectOptions(screen.getByLabelText('Sort collection'), 'copies');
  await waitFor(() => expect(fetcher.mock.calls.some(([url]) => {
    const params = new URL(url, 'https://miniapp.example').searchParams;
    return params.get('duplicates') === 'true' && params.get('sort') === 'copies' && !params.has('cursor');
  })).toBe(true));
  expect(fetcher.mock.calls.every(([url]) => url.startsWith('/api/inventory/pages?'))).toBe(true);
});

it('resets paging when changing filters and describes an empty duplicates view', async () => {
  const fetcher = vi.fn((url: string) => {
    const params = new URL(url, 'https://miniapp.example').searchParams;
    const filtered = params.get('duplicates') === 'true';
    const page = params.get('cursor') === 'next' ? 2 : 1;
    return Promise.resolve(reply({ items: filtered ? [] : [first], total: filtered ? 0 : 48, page, limit: 24, page_cursor: 'first', next_cursor: !filtered && page === 1 ? 'next' : null }));
  });
  vi.stubGlobal('fetch', fetcher);
  const user = userEvent.setup();
  render(<Catalog {...props} />);
  await screen.findByRole('button', { name: 'View Aiko' });
  await user.click(screen.getByRole('button', { name: 'Next page' }));
  await screen.findByText('Page 2');
  await user.click(screen.getByRole('button', { name: 'Duplicates' }));
  expect(await screen.findByText('No duplicates yet')).toBeInTheDocument();
  const filteredCalls = fetcher.mock.calls.filter(([url]) => new URL(url, 'https://miniapp.example').searchParams.get('duplicates') === 'true');
  expect(filteredCalls.length).toBeGreaterThan(0);
  expect(filteredCalls.every(([url]) => !new URL(url, 'https://miniapp.example').searchParams.has('cursor'))).toBe(true);
});

it('ignores a late response from an earlier collection filter', async () => {
  let resolveInitial: ((value: Response) => void) | undefined;
  const fetcher = vi.fn((url: string) => {
    const params = new URL(url, 'https://miniapp.example').searchParams;
    if (params.get('duplicates') === 'true') return Promise.resolve(reply({ items: [duplicate], total: 1, page: 1, limit: 24, page_cursor: 'first', next_cursor: null }));
    return new Promise<Response>(resolve => { resolveInitial = resolve; });
  });
  vi.stubGlobal('fetch', fetcher);
  const user = userEvent.setup();
  render(<Catalog {...props} />);
  await user.click(screen.getByRole('button', { name: 'Duplicates' }));
  expect(await screen.findByRole('button', { name: 'View Ren' })).toBeInTheDocument();
  resolveInitial?.(reply({ items: [first], total: 1, page: 1, limit: 24, page_cursor: 'first', next_cursor: null }));
  await waitFor(() => expect(screen.queryByRole('button', { name: 'View Aiko' })).not.toBeInTheDocument());
  expect(screen.getByRole('button', { name: 'View Ren' })).toBeInTheDocument();
});

it('does not jump back to page one when the initial empty search timer finishes', async () => {
  const fetcher = vi.fn((url: string) => {
    const params = new URL(url, 'https://miniapp.example').searchParams;
    const page = params.get('cursor') === 'next' ? 2 : 1;
    return Promise.resolve(reply({ items: [first], total: 48, page, limit: 24, page_cursor: 'first', next_cursor: page === 1 ? 'next' : null }));
  });
  vi.stubGlobal('fetch', fetcher);
  const user = userEvent.setup();
  render(<Catalog {...props} />);
  await screen.findByRole('button', { name: 'View Aiko' });
  await user.click(screen.getByRole('button', { name: 'Next page' }));
  await screen.findByText('Page 2');
  await new Promise(resolve => setTimeout(resolve, 350));
  expect(screen.getByText('Page 2')).toBeInTheDocument();
});
