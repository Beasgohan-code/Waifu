import { useEffect } from 'react';
import type { CollectionButtonEmoji } from './types';

export const fallbackCollectionButtonEmoji: CollectionButtonEmoji = { fallback: '💎', custom_emoji_id: null };

/** An optional, navigation-only native shortcut. Browser controls remain available. */
export function useTelegramCollectionButton(enabled: boolean, emoji: CollectionButtonEmoji, onOpen: () => void) {
  useEffect(() => {
    const app = window.Telegram?.WebApp;
    const button = app?.MainButton;
    if (!enabled || !button || typeof button.setParams !== 'function' ||
        typeof button.onClick !== 'function' || typeof button.offClick !== 'function' ||
        typeof button.show !== 'function' || typeof button.hide !== 'function') return;

    let supported = false;
    try { supported = app?.isVersionAtLeast?.('9.5') === true; } catch { /* Older bridges may throw. */ }
    const id = supported && typeof emoji.custom_emoji_id === 'string' && /^\d{10,20}$/.test(emoji.custom_emoji_id)
      ? emoji.custom_emoji_id : null;
    const fallback = typeof emoji.fallback === 'string' && emoji.fallback.length ? emoji.fallback : '💎';
    const plain = { text: `${fallback} Open collection`, ...(supported ? { icon_custom_emoji_id: '' } : {}) };
    let subscribed = false;
    try {
      try { button.setParams(id ? { text: 'Open collection', icon_custom_emoji_id: id } : plain); }
      catch {
        if (!id) throw new Error('Native button unavailable');
        button.setParams(plain);
      }
      subscribed = true;
      button.onClick(onOpen);
      button.show();
    } catch {
      // A missing/broken bridge must never make the Mini App unusable.
      if (subscribed) { try { button.offClick(onOpen); } catch { /* ignore */ } }
      try { button.hide(); } catch { /* ignore */ }
      return;
    }
    return () => {
      try { button.offClick(onOpen); } catch { /* ignore */ }
      try { button.hide(); } catch { /* ignore */ }
    };
  }, [enabled, emoji, onOpen]);
}
