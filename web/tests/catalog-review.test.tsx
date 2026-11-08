import '@testing-library/jest-dom/vitest';
import { cleanup, render, screen, waitFor, within } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import { api, ApiError } from '../src/api';
import App from '../src/App';
import Admin from '../src/pages/Admin';

beforeEach(() => {
  vi.restoreAllMocks();
  vi.spyOn(api, 'get').mockResolvedValue({ revision: 'a'.repeat(64), character: null });
});
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
    expect(onDraftChange).toHaveBeenLastCalledWith({ blocked: false });
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
    expect(api.get).toHaveBeenCalledWith('/admin/characters/aiko/edit-state');
    expect(put).toHaveBeenCalledWith('/admin/characters/aiko/reviewed', {
      character: { id: 'aiko', name: 'Aiko Tsukino', category: 'Moonlit Chronicles', rarity: 'legendary',
        image_url: 'https://images.example/aiko.jpg', description: 'A thoughtful collector.', active: true },
      expected_revision: 'a'.repeat(64),
    });
    expect(notify).toHaveBeenCalledWith('Character saved to the catalogue.');
    expect(refresh).toHaveBeenCalledOnce();
    expect(onDraftChange).toHaveBeenLastCalledWith(null);
  });

  it('loads existing metadata without publishing or leaving a dirty draft', async () => {
    vi.mocked(api.get).mockResolvedValue({ revision: 'c'.repeat(64), character: {
      id: 'aiko', name: 'Original Aiko', category: 'Old series', rarity: 'epic', image_url: '/art/aiko.webp', description: 'Existing text', active: false,
    } });
    const put = vi.spyOn(api, 'put').mockResolvedValue({});
    const draft = vi.fn(); render(<Admin notify={vi.fn()} refresh={vi.fn()} onDraftChange={draft} />);
    const user = userEvent.setup();
    await user.type(screen.getByLabelText('Character ID'), 'aiko');
    await user.click(screen.getByRole('button', { name: 'Load existing details' }));
    expect(screen.getByLabelText('Name')).toHaveValue('Original Aiko');
    expect(screen.getByLabelText('Name')).toHaveFocus();
    expect(screen.getByLabelText('Series / category')).toHaveValue('Old series');
    expect(screen.getByLabelText('Rarity')).toHaveValue('epic');
    expect(screen.getByLabelText('Available in the catalogue and for spawning')).not.toBeChecked();
    expect(screen.getByRole('status')).toHaveTextContent(/no changes have been published/i);
    expect(draft).toHaveBeenLastCalledWith(null);
    const unload = new Event('beforeunload', { cancelable: true }); window.dispatchEvent(unload);
    expect(unload.defaultPrevented).toBe(false);
    expect(put).not.toHaveBeenCalled();
    await user.type(screen.getByLabelText('Name'), ' revised');
    expect(draft).toHaveBeenLastCalledWith({ blocked: false });
  });

  it('does not replace typed details when loading is unavailable or the ID is missing', async () => {
    render(<Admin notify={vi.fn()} refresh={vi.fn()} />);
    const user = userEvent.setup();
    await user.type(screen.getByLabelText('Character ID'), 'aiko');
    await user.type(screen.getByLabelText('Name'), 'Keep this draft');
    expect(screen.getByRole('button', { name: 'Load existing details' })).toBeDisabled();
    expect(api.get).not.toHaveBeenCalled();
    await user.clear(screen.getByLabelText('Name'));
    await user.click(screen.getByRole('button', { name: 'Load existing details' }));
    expect(screen.getByRole('alert')).toHaveTextContent(/no existing character/i);
    expect(screen.getByLabelText('Character ID')).toHaveValue('aiko');
    expect(screen.getByLabelText('Name')).toHaveValue('');
  });

  it('previews existing motion media only on request and warns before replacing its URL', async () => {
    const video = 'https://files.catbox.moe/clip.mp4';
    vi.mocked(api.get).mockResolvedValue({ revision: 'd'.repeat(64), character: {
      id: 'aiko', name: 'Motion Aiko', category: 'Sky', rarity: 'amv-edition', image_url: video,
      description: '', active: true, media_type: 'video',
    } });
    render(<Admin notify={vi.fn()} refresh={vi.fn()} />);
    const user = userEvent.setup();
    await user.type(screen.getByLabelText('Character ID'), 'aiko');
    await user.click(screen.getByRole('button', { name: 'Load existing details' }));
    expect(screen.getByRole('status')).toHaveTextContent(/keep its URL unchanged/i);
    await user.click(screen.getByRole('button', { name: 'Review character' }));
    expect(screen.getByText('Video')).toBeInTheDocument();
    expect(screen.queryByLabelText('Artwork preview for Motion Aiko')).not.toBeInTheDocument();
    await user.click(screen.getByRole('button', { name: 'Load artwork preview' }));
    const preview = screen.getByLabelText('Artwork preview for Motion Aiko');
    expect(preview.tagName).toBe('VIDEO');
    expect(preview).toHaveAttribute('preload', 'metadata');
    expect(preview).not.toHaveAttribute('autoplay');
    await user.click(screen.getByRole('button', { name: /Edit details/ }));
    await user.clear(screen.getByLabelText('Artwork URL'));
    await user.paste('https://images.example/new.jpg');
    await user.click(screen.getByRole('button', { name: 'Review character' }));
    expect(screen.getByRole('status')).toHaveTextContent(/removes its Telegram file reference/i);
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

  it('displays the existing record before confirming and checks the exact internal ID', async () => {
    vi.mocked(api.get).mockResolvedValue({
      revision: 'b'.repeat(64),
      character: { id: 'aiko', name: 'Previous name', category: 'Old series', rarity: 'epic', image_url: '/art/old.webp', description: 'Current description', active: false },
    });
    const put = vi.spyOn(api, 'put').mockResolvedValue({});
    render(<Admin notify={vi.fn()} refresh={vi.fn()} />);
    const user = await fillDraft();
    await user.click(screen.getByRole('button', { name: /Review character/ }));
    expect(screen.getByText('Current record for this ID')).toBeInTheDocument();
    expect(screen.getByText('Previous name')).toBeInTheDocument();
    expect(screen.getByText('Old series')).toBeInTheDocument();
    expect(screen.getByText('Epic (legacy)')).toBeInTheDocument();
    expect(screen.getByText('Inactive')).toBeInTheDocument();
    await user.click(screen.getByRole('checkbox', { name: /I understand an existing ID/ }));
    await user.click(screen.getByRole('button', { name: /Confirm & save/ }));
    expect(put).toHaveBeenCalledWith('/admin/characters/aiko/reviewed', expect.objectContaining({ expected_revision: 'b'.repeat(64) }));
  });

  it('keeps the draft and never saves when the current record cannot be checked', async () => {
    vi.mocked(api.get).mockRejectedValue(new Error('unavailable'));
    const put = vi.spyOn(api, 'put').mockResolvedValue({});
    render(<Admin notify={vi.fn()} refresh={vi.fn()} />);
    const user = await fillDraft();
    await user.click(screen.getByRole('button', { name: /Review character/ }));
    expect(screen.getByRole('alert')).toHaveTextContent(/could not check/i);
    expect(screen.getByLabelText('Name')).toHaveValue('Aiko Tsukino');
    expect(put).not.toHaveBeenCalled();
  });

  it('requires a fresh review after a conflict without resubmitting automatically', async () => {
    const get = vi.mocked(api.get);
    const put = vi.spyOn(api, 'put').mockRejectedValue(new ApiError(409, 'Catalog entry changed'));
    render(<Admin notify={vi.fn()} refresh={vi.fn()} />);
    const user = await fillDraft();
    await user.click(screen.getByRole('button', { name: /Review character/ }));
    await user.click(screen.getByRole('checkbox', { name: /I understand an existing ID/ }));
    await user.click(screen.getByRole('button', { name: /Confirm & save/ }));
    expect(screen.getByRole('alert')).toHaveTextContent(/review again/i);
    expect(screen.getByRole('button', { name: /Confirm & save/ })).toBeDisabled();
    expect(put).toHaveBeenCalledOnce();
    await user.click(screen.getByRole('button', { name: /Edit details/ }));
    await user.click(screen.getByRole('button', { name: /Review character/ }));
    expect(get).toHaveBeenCalledTimes(2);
    expect(screen.getByRole('button', { name: /Confirm & save/ })).toBeDisabled();
    expect(put).toHaveBeenCalledOnce();
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

  it.each([new Error('timeout'), new ApiError(408, 'Gateway timed out')])('does not retry an unconfirmed write automatically', async failure => {
    const put = vi.spyOn(api, 'put').mockRejectedValue(failure);
    const notify = vi.fn();
    render(<Admin notify={notify} refresh={vi.fn()} />);
    const user = await fillDraft();
    await user.click(screen.getByRole('button', { name: /Review character/ }));
    await user.click(screen.getByRole('checkbox', { name: /I understand an existing ID/ }));
    await user.click(screen.getByRole('button', { name: /Confirm & save/ }));
    expect(screen.getByRole('alert')).toHaveTextContent('Check the catalogue before trying again');
    expect(screen.getByRole('button', { name: /Confirm & save/ })).toBeDisabled();
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

it('prevents discarding a catalogue draft while its save is still in flight', async () => {
  const player = { id: 42, first_name: 'Mika', username: null, points: 0, xp: 0, level: 1, catch_count: 0, unique_characters: 0, catalog_size: 0, streak: 0, last_daily: null, is_operator: true };
  vi.mocked(api.get).mockImplementation(async path => {
    if (path === '/bootstrap') return { preview: false } as never;
    if (path === '/me') return player as never;
    if (path.endsWith('/edit-state')) return { revision: 'a'.repeat(64), character: null } as never;
    return [] as never;
  });
  let release: ((value: unknown) => void) | undefined;
  vi.spyOn(api, 'put').mockImplementation(() => new Promise(resolve => { release = resolve; }));
  vi.stubGlobal('scrollTo', vi.fn());
  window.history.replaceState(null, '', '#admin');
  render(<App />);
  await screen.findByLabelText('Character ID');
  const user = await fillDraft();
  await user.click(screen.getByRole('button', { name: /Review character/ }));
  await user.click(screen.getByRole('checkbox', { name: /I understand an existing ID/ }));
  await user.click(screen.getByRole('button', { name: /Confirm & save/ }));
  const discover = within(screen.getByRole('navigation', { name: 'Main navigation' })).getByRole('button', { name: 'Discover' });
  await user.click(discover);
  const region = screen.getByRole('region', { name: 'Discard catalogue draft' });
  expect(region).toHaveTextContent(/operation is in progress/i);
  expect(within(region).getByRole('button', { name: 'Discard and continue' })).toBeDisabled();
  release?.({});
  await waitFor(() => expect(screen.queryByRole('region', { name: 'Discard catalogue draft' })).not.toBeInTheDocument());
  expect(screen.getByLabelText('Name')).toHaveValue('Aiko Tsukino');
});
