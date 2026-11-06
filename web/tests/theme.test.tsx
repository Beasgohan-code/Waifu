import { act, renderHook } from '@testing-library/react';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import { useTheme } from '../src/theme';

type ThemeListener = () => void;
let listener: ThemeListener | undefined;
const telegram = {
  initData: '',
  colorScheme: 'dark' as 'dark' | 'light',
  themeParams: { bg_color: '#101010', secondary_bg_color: '#202020', text_color: '#ffffff', hint_color: '#bbbbbb' } as Record<string, string>,
  onEvent: vi.fn((_name: string, callback: ThemeListener) => { listener = callback; }),
  offEvent: vi.fn(),
};

beforeEach(() => {
  window.localStorage.clear();
  telegram.colorScheme = 'dark';
  telegram.themeParams = { bg_color: '#101010', secondary_bg_color: '#202020', text_color: '#ffffff', hint_color: '#bbbbbb' };
  window.Telegram = { WebApp: telegram };
  listener = undefined;
});
afterEach(() => {
  delete window.Telegram;
  document.documentElement.removeAttribute('style');
  document.documentElement.removeAttribute('data-theme');
});

describe('Telegram theme preference', () => {
  it('follows Telegram changes without persisting an implicit choice', () => {
    const { result } = renderHook(() => useTheme());
    expect(result.current.theme).toBe('dark');
    expect(window.localStorage.getItem('summon-theme')).toBeNull();
    expect(document.documentElement.style.getPropertyValue('--bg')).toBe('#101010');
    act(() => {
      telegram.colorScheme = 'light';
      telegram.themeParams = { bg_color: '#fafafa', secondary_bg_color: '#ffffff', text_color: '#101010' };
      listener?.();
    });
    expect(result.current.theme).toBe('light');
    expect(document.documentElement.dataset.theme).toBe('light');
    expect(document.documentElement.style.getPropertyValue('--bg')).toBe('#fafafa');
    expect(window.localStorage.getItem('summon-theme')).toBeNull();
  });

  it('persists only explicit choices and allows following Telegram again', () => {
    const { result } = renderHook(() => useTheme());
    act(() => result.current.chooseTheme('light'));
    expect(window.localStorage.getItem('summon-theme')).toBe('light');
    expect(document.documentElement.style.getPropertyValue('--bg')).toBe('');
    act(() => { telegram.colorScheme = 'dark'; listener?.(); });
    expect(result.current.theme).toBe('light');
    act(() => result.current.chooseTheme('telegram'));
    expect(window.localStorage.getItem('summon-theme')).toBeNull();
    expect(result.current.theme).toBe('dark');
    expect(document.documentElement.style.getPropertyValue('--bg')).toBe('#101010');
  });

  it('ignores invalid and low-contrast Telegram colors', () => {
    telegram.themeParams = { bg_color: 'url(https://example.test)', text_color: '#ffffff', hint_color: '#ffffff' };
    const { result } = renderHook(() => useTheme());
    expect(document.documentElement.style.getPropertyValue('--bg')).toBe('');
    act(() => { telegram.themeParams = { bg_color: '#ffffff', text_color: '#eeeeee' }; listener?.(); });
    expect(result.current.theme).toBe('dark');
    expect(document.documentElement.style.getPropertyValue('--bg')).toBe('');
  });
});
