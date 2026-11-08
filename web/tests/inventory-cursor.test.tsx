import { cleanup, render, screen, waitFor } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { afterEach, expect, it, vi } from 'vitest';
import Catalog from '../src/pages/Catalog';

const props = { mode: 'collection' as const, preview: false, revision: 0, onOpen: vi.fn(), onFavorite: vi.fn(), onDiscover: vi.fn() };
const character = (page: number) => ({ id: `item-${page}`, name: `Character ${page}`, category: 'Test', rarity: 'rare', image_url: '/art/aiko.webp', description: '', quantity: 1, favorite: false, wishlisted: false, active: true });
const ok = (data: unknown) => new Response(JSON.stringify({ data }), { status: 200, headers: { 'Content-Type': 'application/json' } });
const failed = (status: number, message: string) => new Response(JSON.stringify({ error: { message } }), { status, headers: { 'Content-Type': 'application/json' } });

afterEach(() => { cleanup(); vi.unstubAllGlobals(); });

it('uses signed cursors to reach entries after page 25, and can go back', async () => {
  const fetcher = vi.fn((url: string) => {
    const params = new URL(url, 'https://miniapp.example').searchParams;
    const cursor = params.get('cursor');
    const page = cursor ? Number(cursor.replace('cursor-', '')) + 1 : 1;
    return Promise.resolve(ok({ items: [character(page)], total: 650, page, limit: 24, page_cursor: page === 1 ? 'start' : null, next_cursor: page < 28 ? `cursor-${page}` : null }));
  });
  vi.stubGlobal('fetch', fetcher);
  const user = userEvent.setup();
  render(<Catalog {...props} />);
  await screen.findByRole('button', { name: 'View Character 1' });
  for (let page = 2; page <= 28; page++) {
    await user.click(screen.getByRole('button', { name: 'Next page' }));
    await screen.findByRole('button', { name: `View Character ${page}` });
  }
  expect(screen.getByText('Page 28')).toBeInTheDocument();
  expect(screen.getByRole('button', { name: 'Next page' })).toBeDisabled();
  expect(fetcher.mock.calls.some(([url]) => url.includes('cursor=cursor-26'))).toBe(true);
  expect(fetcher.mock.calls.every(([url]) => url.startsWith('/api/inventory/pages?'))).toBe(true);
  await user.click(screen.getByRole('button', { name: 'Previous page' }));
  expect(await screen.findByRole('button', { name: 'View Character 27' })).toBeInTheDocument();
}, 15000);

it('resets cursors on filters, sort, and collection mutations', async () => {
  const fetcher = vi.fn((url: string) => {
    const params = new URL(url, 'https://miniapp.example').searchParams;
    const page = params.has('cursor') ? 2 : 1;
    return Promise.resolve(ok({ items: [character(page)], total: 48, page, limit: 24, page_cursor: page === 1 ? 'start' : null, next_cursor: page === 1 ? 'next' : null }));
  });
  vi.stubGlobal('fetch', fetcher);
  const user = userEvent.setup();
  const view = render(<Catalog {...props} />);
  await screen.findByRole('button', { name: 'View Character 1' });
  await user.click(screen.getByRole('button', { name: 'Next page' }));
  await screen.findByRole('button', { name: 'View Character 2' });
  await user.click(screen.getByRole('button', { name: 'Favorites' }));
  await screen.findByRole('button', { name: 'View Character 1' });
  expect(new URL(fetcher.mock.calls.at(-1)![0], 'https://miniapp.example').searchParams.has('cursor')).toBe(false);
  await user.selectOptions(screen.getByLabelText('Sort collection'), 'copies');
  await waitFor(() => expect(new URL(fetcher.mock.calls.at(-1)![0], 'https://miniapp.example').searchParams.get('sort')).toBe('copies'));
  await user.click(screen.getByRole('button', { name: 'Next page' }));
  await screen.findByRole('button', { name: 'View Character 2' });
  view.rerender(<Catalog {...props} revision={1} />);
  await screen.findByRole('button', { name: 'View Character 1' });
  expect(new URL(fetcher.mock.calls.at(-1)![0], 'https://miniapp.example').searchParams.has('cursor')).toBe(false);
});

it('offers an explicit refresh when snapshot history expires', async () => {
  let firstReads = 0;
  const fetcher = vi.fn((url: string) => {
    const params = new URL(url, 'https://miniapp.example').searchParams;
    if (params.has('cursor')) return Promise.resolve(failed(409, 'Collection snapshot expired; refresh the collection'));
    firstReads++;
    return Promise.resolve(ok({ items: [character(1)], total: 48, page: 1, limit: 24, page_cursor: 'start', next_cursor: 'next' }));
  });
  vi.stubGlobal('fetch', fetcher);
  const user = userEvent.setup();
  render(<Catalog {...props} />);
  await screen.findByRole('button', { name: 'View Character 1' });
  await user.click(screen.getByRole('button', { name: 'Next page' }));
  expect(await screen.findByText(/snapshot expired/i)).toBeInTheDocument();
  await user.click(screen.getByRole('button', { name: 'Refresh collection' }));
  expect(await screen.findByRole('button', { name: 'View Character 1' })).toBeInTheDocument();
  expect(firstReads).toBeGreaterThanOrEqual(2);
});

it('offers the explicitly limited legacy page mode if snapshots are unavailable', async () => {
  const fetcher = vi.fn((url: string) => url.startsWith('/api/inventory/pages?')
    ? Promise.resolve(failed(503, 'Stable collection pages are unavailable; use standard pages'))
    : Promise.resolve(ok({ items: [character(1)], total: 1000, page: 1, limit: 24 })));
  vi.stubGlobal('fetch', fetcher);
  const user = userEvent.setup();
  render(<Catalog {...props} />);
  await screen.findByText(/stable collection pages are unavailable/i);
  await user.click(screen.getByRole('button', { name: /use standard pages/i }));
  expect(await screen.findByRole('button', { name: 'View Character 1' })).toBeInTheDocument();
  expect(screen.getByText(/limited to the first 600/i)).toBeInTheDocument();
  expect(fetcher.mock.calls.some(([url]) => url.startsWith('/api/inventory?page=1'))).toBe(true);
});

it('replays the original first-page snapshot when navigating back', async () => {
  const fetcher = vi.fn((url: string) => {
    const params = new URL(url, 'https://miniapp.example').searchParams;
    const cursor = params.get('cursor');
    const page = cursor === 'next' ? 2 : 1;
    return Promise.resolve(ok({ items: [character(page)], total: 48, page, limit: 24, page_cursor: page === 1 ? 'start' : null, next_cursor: page === 1 ? 'next' : null }));
  });
  vi.stubGlobal('fetch', fetcher);
  const user = userEvent.setup();
  render(<Catalog {...props} />);
  await screen.findByRole('button', { name: 'View Character 1' });
  await user.click(screen.getByRole('button', { name: 'Next page' }));
  await screen.findByRole('button', { name: 'View Character 2' });
  await user.click(screen.getByRole('button', { name: 'Previous page' }));
  await screen.findByRole('button', { name: 'View Character 1' });
  expect(new URL(fetcher.mock.calls.at(-1)![0], 'https://miniapp.example').searchParams.get('cursor')).toBe('start');
});
