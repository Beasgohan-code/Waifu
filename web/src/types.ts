import type { Rarity } from './rarities';
export type { Rarity } from './rarities';

export interface Character {
  id: string;
  name: string;
  category: string;
  rarity: Rarity;
  image_url: string;
  media_type?: 'photo' | 'video' | 'animation';
  number?: number;
  description: string;
  quantity: number;
  favorite: boolean;
  wishlisted: boolean;
  active: boolean;
}

export interface Page<T> { items: T[]; page: number; limit: number; total: number }
export interface Profile {
  id: number;
  first_name: string;
  username: string | null;
  points: number;
  xp: number;
  level: number;
  catch_count: number;
  unique_characters: number;
  catalog_size: number;
  streak: number;
  best_streak?: number;
  last_daily: string | null;
  is_operator: boolean;
}
export interface CollectionGoal { id: string; title: string; current: number; target: number; completed: boolean }
export interface CollectionInsights {
  unique_characters: number;
  total_copies: number;
  duplicate_copies: number;
  favorite_characters: number;
  active_owned: number;
  catalog_size: number;
  completion_percent: number | null;
  rarity_breakdown: { rarity: Rarity | 'unknown'; unique: number; copies: number }[];
  goals: CollectionGoal[];
}
export interface Achievement { id: string; title: string; description: string; current: number; target: number; completed: boolean }
export interface DailyQuest { id: string; title: string; description: string; completed: boolean; reward: string }
export interface DailyStatus { state: 'available' | 'claimed' | 'unavailable'; server_time: string; next_reset_at: string }
export interface Progression { achievements: Achievement[]; daily: DailyQuest[]; daily_status?: DailyStatus }
export interface Leader { name: string; catch_count: number; level: number }
export interface Trade {
  id: string; sender_id: number; receiver_id: number; offer_id: string; request_id: string;
  status: 'pending' | 'completed' | 'cancelled' | 'expired'; created_at: string; expires_at: string;
}
export interface Gift {
  id: string; sender_id: number; receiver_id: number; character_id: string;
  status: 'pending' | 'completed' | 'cancelled' | 'expired'; created_at: string; expires_at: string;
}
export interface GroupSettings {
  spawn_enabled: boolean; spawn_every: number; spawn_cooldown_seconds: number; spawn_lifetime_seconds: number;
  welcome_enabled: boolean; welcome_text: string; goodbye_enabled: boolean; goodbye_text: string; rules: string; link_filter: boolean;
  flood_enabled: boolean; flood_limit: number; flood_window_seconds: number;
  duplicate_enabled: boolean; duplicate_limit: number; mention_limit: number;
  blocked_keywords: string[]; warning_limit: number;
}
export interface Group {
  id: number; title: string; total_messages: number; settings: GroupSettings;
  permissions: { can_change_info: boolean; can_restrict_members: boolean };
  recent_activity?: Activity[];
}
export interface Activity { kind: string; action: string | null; reason: string | null; actor_id?: number; target_id?: number; warning_id?: string | null; note_name?: string | null; reply_name?: string | null; created_at: string }
export interface GroupNote { name: string; content: string; revision: string; created_at: string; updated_at: string; created_by: number; updated_by: number }
export interface GroupAutoReply { name: string; trigger: string; response: string; enabled: boolean; revision: string; created_at: string; updated_at: string; created_by: number; updated_by: number }
export interface WarningRecord { id: string; actor_id: number; reason: string; created_at: string }
export interface WarningPage { items: WarningRecord[]; active_count: number; next_cursor: string | null }

export type View = 'home' | 'collection' | 'catalog' | 'trades' | 'progress' | 'leaderboard' | 'groups' | 'profile' | 'settings' | 'admin';

export interface CollectionButtonEmoji { fallback: string; custom_emoji_id: string | null }
interface TelegramBottomButton {
  setParams: (params: { text: string; icon_custom_emoji_id?: string }) => void;
  onClick: (callback: () => void) => void;
  offClick: (callback: () => void) => void;
  show: () => void;
  hide: () => void;
}

declare global {
  interface Window {
    Telegram?: { WebApp?: {
      initData: string;
      colorScheme?: 'light' | 'dark';
      themeParams?: Record<string, string>;
      MainButton?: TelegramBottomButton;
      isVersionAtLeast?: (version: string) => boolean;
      ready?: () => void;
      expand?: () => void;
      onEvent?: (name: string, callback: () => void) => void;
      offEvent?: (name: string, callback: () => void) => void;
    }};
  }
}
