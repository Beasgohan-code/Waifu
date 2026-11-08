import { cleanup, render, screen } from '@testing-library/react';
import { afterEach, describe, expect, it, vi } from 'vitest';
import { CharacterCard } from '../src/components';
import type { Character } from '../src/types';

const base: Character = {
  id: 'aiko', name: 'Aiko', category: 'Sky', rarity: 'rare',
  image_url: 'https://files.catbox.moe/aiko.jpg', description: '',
  quantity: 0, favorite: false, wishlisted: false, active: true,
};

afterEach(cleanup);

describe('catalogue media', () => {
  it('keeps ordinary and legacy photo artwork as images', () => {
    render(<CharacterCard character={base} onOpen={vi.fn()} preview={false} />);
    expect(screen.getByRole('img', { name: 'Aiko' })).toHaveAttribute('src', base.image_url);
  });

  it('uses an accessible video element for moving MP4 artwork', () => {
    const character = { ...base, media_type: 'video' as const, image_url: 'https://files.catbox.moe/clip.mp4' };
    render(<CharacterCard character={character} onOpen={vi.fn()} preview={false} />);
    const video = screen.getByLabelText('Aiko video artwork');
    expect(video.tagName).toBe('VIDEO');
    expect(video).toHaveAttribute('src', character.image_url);
    expect(video).not.toHaveAttribute('autoplay');
  });

  it('shows hosted GIF animations as images rather than invalid video sources', () => {
    const character = { ...base, media_type: 'animation' as const, image_url: 'https://files.catbox.moe/clip.gif' };
    render(<CharacterCard character={character} onOpen={vi.fn()} preview={false} />);
    expect(screen.getByRole('img', { name: 'Aiko' })).toHaveAttribute('src', character.image_url);
  });
});
