import { act, cleanup, render, renderHook, screen, waitFor } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import App from '../src/App';
import { afterEach, describe, expect, it, vi } from 'vitest';
import { useTelegramCollectionButton } from '../src/telegram-collection-button';

const emoji = { fallback: '💎', custom_emoji_id: '6215348101478097302' };

afterEach(() => { cleanup(); delete window.Telegram; vi.unstubAllGlobals(); window.history.replaceState(null, '', '/'); });

function bridge(versionSupported = true) {
  const callbacks = new Set<() => void>();
  const button = {
    setParams: vi.fn(),
    onClick: vi.fn((handler: () => void) => { callbacks.add(handler); }),
    offClick: vi.fn((handler: () => void) => { callbacks.delete(handler); }),
    show: vi.fn(),
    hide: vi.fn(),
  };
  window.Telegram = { WebApp: {
    initData: '', MainButton: button, isVersionAtLeast: vi.fn(() => versionSupported),
  } };
  return { button, callbacks };
}

describe('Telegram native collection button', () => {
  it('uses the supplied string ID on supported clients, navigates once, and removes its handler', () => {
    const { button, callbacks } = bridge();
    const navigate = vi.fn();
    const { unmount } = renderHook(() => useTelegramCollectionButton(true, emoji, navigate));
    expect(button.setParams).toHaveBeenCalledWith(expect.objectContaining({
      text: 'Open collection', icon_custom_emoji_id: '6215348101478097302',
    }));
    expect(button.show).toHaveBeenCalled();
    act(() => callbacks.forEach(handler => handler()));
    expect(navigate).toHaveBeenCalledTimes(1);
    unmount();
    expect(button.offClick).toHaveBeenCalledWith(navigate);
    expect(button.hide).toHaveBeenCalled();
    expect(callbacks.size).toBe(0);
  });

  it('uses the Unicode fallback for older clients and never sends unsupported icon fields', () => {
    const { button } = bridge(false);
    renderHook(() => useTelegramCollectionButton(true, emoji, vi.fn()));
    expect(button.setParams).toHaveBeenCalledWith(expect.objectContaining({ text: '💎 Open collection' }));
    expect(button.setParams.mock.calls[0][0]).not.toHaveProperty('icon_custom_emoji_id');
  });

  it('honors the disabled feature flag and clears any previously set icon on supported clients', () => {
    const { button } = bridge();
    renderHook(() => useTelegramCollectionButton(true, { ...emoji, custom_emoji_id: null }, vi.fn()));
    expect(button.setParams).toHaveBeenCalledWith(expect.objectContaining({
      text: '💎 Open collection', icon_custom_emoji_id: '',
    }));
  });

  it('uses a safe label for malformed IDs without sending them to the bridge', () => {
    const { button } = bridge();
    renderHook(() => useTelegramCollectionButton(true, { ...emoji, custom_emoji_id: 'bad-id' }, vi.fn()));
    expect(button.setParams).toHaveBeenCalledWith(expect.objectContaining({
      text: '💎 Open collection', icon_custom_emoji_id: '',
    }));
  });

  it('removes its click handler when the button should not be visible', () => {
    const { button, callbacks } = bridge();
    const navigate = vi.fn();
    const { rerender } = renderHook(({ enabled }) => useTelegramCollectionButton(enabled, emoji, navigate), {
      initialProps: { enabled: true },
    });
    expect(callbacks.size).toBe(1);
    rerender({ enabled: false });
    expect(callbacks.size).toBe(0);
    expect(button.hide).toHaveBeenCalled();
  });

  it('falls back to a readable label if the bridge rejects an icon', () => {
    const { button, callbacks } = bridge();
    button.setParams.mockImplementationOnce(() => { throw new Error('unsupported'); });
    renderHook(() => useTelegramCollectionButton(true, emoji, vi.fn()));
    expect(button.setParams).toHaveBeenLastCalledWith(expect.objectContaining({ text: '💎 Open collection' }));
    expect(button.show).toHaveBeenCalled();
    expect(callbacks.size).toBe(1);
  });

  it('cleans up if the native bridge fails after subscribing', () => {
    const { button, callbacks } = bridge();
    button.show.mockImplementationOnce(() => { throw new Error('bridge failed'); });
    expect(() => renderHook(() => useTelegramCollectionButton(true, emoji, vi.fn()))).not.toThrow();
    expect(callbacks.size).toBe(0);
    expect(button.hide).toHaveBeenCalled();
  });

  it('leaves ordinary browsers and incomplete Telegram bridges usable', () => {
    expect(() => renderHook(() => useTelegramCollectionButton(true, emoji, vi.fn()))).not.toThrow();
    window.Telegram = { WebApp: { initData: '' } };
    expect(() => renderHook(() => useTelegramCollectionButton(true, emoji, vi.fn()))).not.toThrow();
  });
});

it('wires the native shortcut to the real collection and hides it while a detail dialog is open', async () => {
  const { button, callbacks } = bridge();
  const profile = { id: 1, first_name: 'Mika', username: null, points: 0, xp: 0, level: 1, catch_count: 0, unique_characters: 0, catalog_size: 1, streak: 0, last_daily: null, is_operator: false };
  const character = { id: 'aiko', name: 'Aiko', category: 'Sky', rarity: 'rare', image_url: '/aiko.png', description: '', quantity: 0, favorite: false, wishlisted: false, active: true };
  const page = { items: [character], total: 1, page: 1, limit: 24 };
  const fetcher = vi.fn((url: string, _options?: RequestInit) => {
    const data = url === '/api/bootstrap' ? { preview: true, collection_button_emoji: emoji } :
      url === '/api/me' ? profile : url === '/api/progression' ? { achievements: [], daily: [] } :
      url === '/api/characters/aiko' ? character : url.startsWith('/api/characters?') ? page :
      url.startsWith('/api/inventory/pages?') ? { ...page, items: [], total: 0, page_cursor: null, next_cursor: null } : {};
    return Promise.resolve(new Response(JSON.stringify({ data }), { status: 200, headers: { 'Content-Type': 'application/json' } }));
  });
  vi.stubGlobal('fetch', fetcher);
  vi.stubGlobal('scrollTo', vi.fn());
  window.history.replaceState(null, '', '#home');
  const user = userEvent.setup();
  render(<App />);
  await waitFor(() => expect(button.show).toHaveBeenCalled());
  expect(button.setParams).toHaveBeenCalledWith(expect.objectContaining({ icon_custom_emoji_id: emoji.custom_emoji_id }));
  await user.click(await screen.findByRole('button', { name: 'View Aiko' }));
  expect(await screen.findByRole('dialog', { name: 'Character details' })).toBeInTheDocument();
  expect(callbacks.size).toBe(0);
  expect(button.hide).toHaveBeenCalled();
  await user.click(screen.getByRole('button', { name: 'Close character details' }));
  await waitFor(() => expect(callbacks.size).toBe(1));
  act(() => callbacks.forEach(handler => handler()));
  expect(await screen.findByRole('heading', { name: 'Your collection.' })).toBeInTheDocument();
  expect(callbacks.size).toBe(0);
  expect(fetcher.mock.calls.every(([, options]) => !options || options.method === 'GET')).toBe(true);
});
