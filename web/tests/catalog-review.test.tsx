import '@testing-library/jest-dom/vitest';
import { cleanup, render, screen, waitFor, within } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import { api, ApiError } from '../src/api';
import App from '../src/App';
import Admin from '../src/pages/Admin';

beforeEach(() => vi.restoreAllMocks());
afterEach(() => { cleanup(); vi.unstubAllGlobals(); window.history.replaceState(null, '', '/'); });

async function fillDraft(image = 'https://images.example/aiko.jpg') {
  const user = userEvent.setup();
  await user.type(screen.getByLabelText('Character ID'), 'aiko');
  await user.type(screen.getByLabelText('Name'), 'Aiko Tsukino');
  await user.type(screen.getByLabelText('Series / category'), 'Moonlit Chronicles');
  await user.selectOptions(screen.getByLabelText('Rarity'), 'legendary');
  await user.click(screen.getByLabelText('Artwork URL'));
  await user.paste(image);
  await user.type(screen.getByLabelText('Description'), 'A thoughtful collector.');
  return user;
}

describe('catalogue review', () => {
  it('reviews without saving or loading remote artwork, then requires explicit confirmation', async () => {
    const put = vi.spyOn(api, 'put').mockResolvedValue({});
    const notify = vi.fn(); const refresh = vi.fn(); const onDraftChange = vi.fn();
    render(<Admin notify={notify} refresh={refresh} onDraftChange={onDraftChange} />);
    const user = await fillDraft();
    expect(onDraftChange).toHaveBeenLastCalledWith(true);
    await user.click(screen.getByRole('button', { name: /Review character/ }));
    expect(put).not.toHaveBeenCalled();
    expect(screen.getByRole('heading', { name: 'Check the details.' })).toBeInTheDocument();
    expect(screen.getByText('Aiko Tsukino')).toBeInTheDocument();
    expect(screen.getByText('Legendary')).toBeInTheDocument();
    expect(screen.queryByRole('img')).not.toBeInTheDocument();
    expect(screen.getByRole('button', { name: /Confirm & save/ })).toBeDisabled();
    await user.click(screen.getByRole('button', { name: 'Load artwork preview' }));
    expect(screen.getByRole('img', { name: 'Artwork preview for Aiko Tsukino' })).toHaveAttribute('src', 'https://images.example/aiko.jpg');
    await user.click(screen.getByRole('checkbox', { name: /I understand an existing ID/ }));
    await user.click(screen.getByRole('button', { name: /Confirm & save/ }));
    expect(put).toHaveBeenCalledOnce();
    expect(put).toHaveBeenCalledWith('/admin/characters/aiko', {
      id: 'aiko', name: 'Aiko Tsukino', category: 'Moonlit Chronicles', rarity: 'legendary',
      image_url: 'https://images.example/aiko.jpg', description: 'A thoughtful collector.', active: true,
    });
    expect(notify).toHaveBeenCalledWith('Character saved to the catalogue.');
    expect(refresh).toHaveBeenCalledOnce();
    expect(onDraftChange).toHaveBeenLastCalledWith(false);
  });

  it('lets an operator revise the draft before publishing', async () => {
    const put = vi.spyOn(api, 'put').mockResolvedValue({});
    render(<Admin notify={vi.fn()} refresh={vi.fn()} />);
    const user = await fillDraft('/art/aiko.webp');
    await user.click(screen.getByRole('button', { name: /Review character/ }));
    await user.click(screen.getByRole('button', { name: /Edit details/ }));
    const name = screen.getByLabelText('Name');
    expect(name).toHaveValue('Aiko Tsukino');
    await user.clear(name); await user.type(name, 'Aiko');
    await user.click(screen.getByRole('button', { name: /Review character/ }));
    expect(screen.getByText('Aiko')).toBeInTheDocument();
    expect(put).not.toHaveBeenCalled();
    expect(screen.getByRole('button', { name: /Confirm & save/ })).toBeDisabled();
  });

  it.each(['http://example.com/image.jpg', 'https://127.0.0.1/private.jpg', 'https://[::1]/private.jpg'])('does not preview or submit unsafe URL %s', async image => {
    const put = vi.spyOn(api, 'put').mockResolvedValue({});
    render(<Admin notify={vi.fn()} refresh={vi.fn()} />);
    const user = await fillDraft(image);
    await user.click(screen.getByRole('button', { name: /Review character/ }));
    expect(screen.getByRole('alert')).toHaveTextContent('public HTTPS');
    expect(screen.queryByRole('img')).not.toBeInTheDocument();
    expect(put).not.toHaveBeenCalled();
  });

  it('shows a definite authorization rejection without claiming an ambiguous write', async () => {
    const put = vi.spyOn(api, 'put').mockRejectedValue(new ApiError(403, 'Catalog operator access required'));
    render(<Admin notify={vi.fn()} refresh={vi.fn()} />);
    const user = await fillDraft();
    await user.click(screen.getByRole('button', { name: /Review character/ }));
    await user.click(screen.getByRole('checkbox', { name: /I understand an existing ID/ }));
    await user.click(screen.getByRole('button', { name: /Confirm & save/ }));
    expect(screen.getByRole('alert')).toHaveTextContent('Catalog operator access required');
    expect(put).toHaveBeenCalledOnce();
  });

  it('allows draft review in read-only preview without sending a save', async () => {
    const put = vi.spyOn(api, 'put').mockResolvedValue({});
    render(<Admin preview notify={vi.fn()} refresh={vi.fn()} />);
    const user = await fillDraft();
    await user.click(screen.getByRole('button', { name: /Review character/ }));
    await user.click(screen.getByRole('checkbox', { name: /I understand an existing ID/ }));
    expect(screen.getByRole('button', { name: /Preview only/ })).toBeDisabled();
    expect(put).not.toHaveBeenCalled();
  });

  it('does not retry an unconfirmed write automatically', async () => {
    const put = vi.spyOn(api, 'put').mockRejectedValue(new Error('timeout'));
    const notify = vi.fn();
    render(<Admin notify={notify} refresh={vi.fn()} />);
    const user = await fillDraft();
    await user.click(screen.getByRole('button', { name: /Review character/ }));
    await user.click(screen.getByRole('checkbox', { name: /I understand an existing ID/ }));
    await user.click(screen.getByRole('button', { name: /Confirm & save/ }));
    expect(screen.getByRole('alert')).toHaveTextContent('Check the catalogue before trying again');
    expect(put).toHaveBeenCalledOnce();
    expect(notify).toHaveBeenCalledWith(expect.stringMatching(/could not be confirmed/), true);
  });
});

it('guards a catalogue draft across app navigation and browser unload', async () => {
  const player = { id: 42, first_name: 'Mika', username: null, points: 0, xp: 0, level: 1, catch_count: 0, unique_characters: 0, catalog_size: 0, streak: 0, last_daily: null, is_operator: true };
  vi.spyOn(api, 'get').mockImplementation(async path => {
    if (path === '/bootstrap') return { preview: false } as never;
    if (path === '/me') return player as never;
    if (path.startsWith('/characters?')) return { items: [], total: 0, page: 1, limit: 24 } as never;
    return [] as never;
  });
  vi.stubGlobal('scrollTo', vi.fn());
  window.history.replaceState(null, '', '#admin');
  const user = userEvent.setup();
  render(<App />);
  const name = await screen.findByLabelText('Name');
  await user.type(name, 'Draft character');
  const unload = new Event('beforeunload', { cancelable: true });
  window.dispatchEvent(unload);
  expect(unload.defaultPrevented).toBe(true);
  const discover = within(screen.getByRole('navigation', { name: 'Main navigation' })).getByRole('button', { name: 'Discover' });
  await user.click(discover);
  let region = screen.getByRole('region', { name: 'Discard catalogue draft' });
  expect(region).toHaveFocus();
  await user.click(within(region).getByRole('button', { name: 'Keep editing' }));
  expect(name).toHaveValue('Draft character');
  await user.click(discover);
  region = screen.getByRole('region', { name: 'Discard catalogue draft' });
  await user.click(within(region).getByRole('button', { name: 'Discard and continue' }));
  await waitFor(() => expect(screen.getByRole('heading', { name: 'Meet the characters.' })).toBeInTheDocument());
});
