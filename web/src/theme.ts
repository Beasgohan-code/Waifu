import { useCallback, useEffect, useState } from 'react';

type Theme = 'light' | 'dark';
type Preference = Theme | 'telegram';
type Snapshot = { scheme: Theme; params: Record<string, string> };

const paletteKeys = ['--bg', '--text', '--panel', '--panel-soft', '--sidebar', '--muted'] as const;
const validColor = (value: string | undefined): value is string => Boolean(value && /^#[\da-f]{6}$/i.test(value));

// Use a supplied palette only where text remains legible against each background.
function luminance(color: string) {
  const values = [1, 3, 5].map(index => parseInt(color.slice(index, index + 2), 16) / 255)
    .map(value => value <= 0.04045 ? value / 12.92 : ((value + 0.055) / 1.055) ** 2.4);
  return 0.2126 * values[0] + 0.7152 * values[1] + 0.0722 * values[2];
}
function readable(foreground: string, background: string) {
  const a = luminance(foreground); const b = luminance(background);
  return (Math.max(a, b) + 0.05) / (Math.min(a, b) + 0.05) >= 4.5;
}
function snapshot(): Snapshot {
  const telegram = window.Telegram?.WebApp;
  return { scheme: telegram?.colorScheme === 'light' ? 'light' : 'dark', params: { ...telegram?.themeParams } };
}
function savedPreference(): Preference {
  try {
    const saved = window.localStorage.getItem('summon-theme');
    return saved === 'dark' || saved === 'light' ? saved : 'telegram';
  } catch { return 'telegram'; }
}

export function useTheme() {
  const [preference, setPreference] = useState<Preference>(savedPreference);
  const [telegram, setTelegram] = useState<Snapshot>(snapshot);
  const theme = preference === 'telegram' ? telegram.scheme : preference;

  useEffect(() => {
    const webApp = window.Telegram?.WebApp;
    const update = () => setTelegram(snapshot());
    webApp?.onEvent?.('themeChanged', update);
    return () => webApp?.offEvent?.('themeChanged', update);
  }, []);

  useEffect(() => {
    const root = document.documentElement;
    root.dataset.theme = theme;
    paletteKeys.forEach(key => root.style.removeProperty(key));
    if (preference === 'telegram') {
      const { bg_color: bg, text_color: text, secondary_bg_color: secondary, hint_color: hint } = telegram.params;
      if (validColor(bg) && validColor(text) && readable(text, bg)) {
        root.style.setProperty('--bg', bg);
        root.style.setProperty('--text', text);
        if (validColor(secondary) && readable(text, secondary)) {
          root.style.setProperty('--panel', secondary);
          root.style.setProperty('--panel-soft', secondary);
          root.style.setProperty('--sidebar', secondary);
        }
        if (validColor(hint) && readable(hint, bg) && (!validColor(secondary) || readable(hint, secondary))) {
          root.style.setProperty('--muted', hint);
        }
      }
    }
    return () => { paletteKeys.forEach(key => root.style.removeProperty(key)); };
  }, [preference, telegram, theme]);

  const chooseTheme = useCallback((choice: Preference) => {
    setPreference(choice);
    try {
      if (choice === 'telegram') window.localStorage.removeItem('summon-theme');
      else window.localStorage.setItem('summon-theme', choice);
    } catch { /* The current choice still applies if storage is disabled. */ }
  }, []);
  const toggleTheme = useCallback(() => chooseTheme(theme === 'dark' ? 'light' : 'dark'), [chooseTheme, theme]);
  return { theme, preference, chooseTheme, toggleTheme };
}
