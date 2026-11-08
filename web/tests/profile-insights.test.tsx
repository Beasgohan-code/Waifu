import '@testing-library/jest-dom/vitest';
import { cleanup, render, screen, waitFor } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import { api } from '../src/api';
import Profile from '../src/pages/Profile';
import type { CollectionInsights, Profile as Player } from '../src/types';

const profile: Player = {
  id: 42, first_name: 'Mika', username: 'mika', points: 840, xp: 425,
  level: 3, catch_count: 8, unique_characters: 4, catalog_size: 3,
  streak: 2, best_streak: 7, last_daily: '2026-01-02', is_operator: false,
};
const insights: CollectionInsights = {
  unique_characters: 4, total_copies: 8, duplicate_copies: 4, favorite_characters: 2,
  active_owned: 2, catalog_size: 3, completion_percent: 66.7,
  rarity_breakdown: [
    { rarity: 'rare', unique: 2, copies: 3 },
    { rarity: 'epic', unique: 1, copies: 3 },
    { rarity: 'unknown', unique: 1, copies: 2 },
  ],
  goals: [
    { id: 'collection-1', title: 'First companion', current: 1, target: 1, completed: true },
    { id: 'collection-5', title: 'A growing collection', current: 4, target: 5, completed: false },
  ],
};

beforeEach(() => vi.restoreAllMocks());
afterEach(cleanup);

describe('private collector profile insights', () => {
  it('shows active completion, owned copies, legacy rarity and current goals', async () => {
    const get = vi.spyOn(api, 'get').mockResolvedValue(insights);
    render(<Profile profile={profile} navigate={vi.fn()} revision={0} />);
    expect(screen.getByRole('status')).toHaveTextContent('Loading collector insights');
    expect(await screen.findByText(/2 of 3 active catalogue characters/)).toHaveTextContent('66.7%');
    expect(screen.getByText('Best recorded: 7 days')).toBeInTheDocument();
    expect(screen.getByText('Extra copies').parentElement).toHaveTextContent('4');
    expect(screen.getByText('Favorites').parentElement).toHaveTextContent('2');
    expect(screen.getByText('Epic (legacy)')).toBeInTheDocument();
    expect(screen.getByText('Uncategorized')).toBeInTheDocument();
    expect(screen.getByText('A growing collection')).toBeInTheDocument();
    expect(screen.getByText(/not permanent rewards/)).toBeInTheDocument();
    expect(get).toHaveBeenCalledWith('/me/collection-insights');
  });

  it('handles an empty active catalogue without claiming a completion percentage', async () => {
    vi.spyOn(api, 'get').mockResolvedValue({ ...insights, active_owned: 0, catalog_size: 0, completion_percent: null, rarity_breakdown: [] });
    render(<Profile profile={profile} navigate={vi.fn()} revision={0} />);
    expect(await screen.findByText('There are no active catalogue characters yet.')).toBeInTheDocument();
    expect(screen.getByText(/Your collection is empty/)).toBeInTheDocument();
    expect(screen.queryByRole('progressbar', { name: 'Active catalogue completion' })).not.toBeInTheDocument();
  });

  it('shows an honest error and retries the same actor-bound endpoint', async () => {
    const get = vi.spyOn(api, 'get').mockRejectedValueOnce(new Error('unavailable')).mockResolvedValueOnce(insights);
    render(<Profile profile={profile} navigate={vi.fn()} revision={0} />);
    expect(await screen.findByRole('alert')).toHaveTextContent('Your collection is unchanged');
    await userEvent.click(screen.getByRole('button', { name: 'Try again' }));
    await waitFor(() => expect(screen.getByText('Epic (legacy)')).toBeInTheDocument());
    expect(get).toHaveBeenCalledTimes(2);
    expect(get).toHaveBeenLastCalledWith('/me/collection-insights');
  });
});
