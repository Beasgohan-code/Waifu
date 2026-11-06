import { Award, ArrowLeftRight, BookOpen, ChevronDown, Compass, Home as HomeIcon, LogOut, Menu, Moon, Search, Settings2, ShieldCheck, Sparkles, Sun, Trophy, UserRound, Users, X } from 'lucide-react';
import { useCallback, useEffect, useRef, useState } from 'react';
import { useTheme } from './theme';
import { api, startSession } from './api';
import { Brand, CharacterDetail, EmptyState } from './components';
import { fallbackCollectionButtonEmoji, useTelegramCollectionButton } from './telegram-collection-button';
import type { Character, CollectionButtonEmoji, Profile, View } from './types';
import Admin from './pages/Admin';
import Catalog from './pages/Catalog';
import Groups from './pages/Groups';
import Home from './pages/Home';
import Leaderboard from './pages/Leaderboard';
import ProfilePage from './pages/Profile';
import Progress from './pages/Progress';
import Trading from './pages/Trading';

type Icon = typeof HomeIcon;
const menu: { view: View; label: string; icon: Icon; group: string }[] = [
  { view: 'home', label: 'Overview', icon: HomeIcon, group: 'YOUR SPACE' },
  { view: 'collection', label: 'My collection', icon: BookOpen, group: 'YOUR SPACE' },
  { view: 'catalog', label: 'Discover', icon: Compass, group: 'YOUR SPACE' },
  { view: 'trades', label: 'Trading', icon: ArrowLeftRight, group: 'YOUR SPACE' },
  { view: 'progress', label: 'Achievements', icon: Award, group: 'YOUR SPACE' },
  { view: 'leaderboard', label: 'Leaderboard', icon: Trophy, group: 'COMMUNITY' },
  { view: 'groups', label: 'Group manager', icon: Users, group: 'COMMUNITY' },
  { view: 'profile', label: 'My profile', icon: UserRound, group: 'ACCOUNT' },
  { view: 'settings', label: 'Settings', icon: Settings2, group: 'ACCOUNT' },
];
const allowedViews = new Set<View>([...menu.map(item => item.view), 'admin']);

function initialView(): View {
  const name = window.location.hash.slice(1) as View;
  return allowedViews.has(name) ? name : 'home';
}

export default function App() {
  const [view, setView] = useState<View>(initialView);
  const [profile, setProfile] = useState<Profile | null>(null);
  const [preview, setPreview] = useState(false);
  const [collectionButtonEmoji, setCollectionButtonEmoji] = useState<CollectionButtonEmoji>(fallbackCollectionButtonEmoji);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState('');
  const [revision, setRevision] = useState(0);
  const [character, setCharacter] = useState<string | null>(null);
  const [dailyBusy, setDailyBusy] = useState(false);
  const [moreOpen, setMoreOpen] = useState(false);
  const [noteDraft, setNoteDraft] = useState(false);
  const [pendingView, setPendingView] = useState<View | null>(null);
  const draftConfirmRef = useRef<HTMLDivElement>(null);
  const draftOpenerRef = useRef<HTMLElement | null>(null);
  const [toast, setToast] = useState<{ message: string; error: boolean } | null>(null);
  const { theme, preference, chooseTheme, toggleTheme } = useTheme();
  const refresh = useCallback(() => setRevision(value => value + 1), []);
  const navigateNow = useCallback((next: View) => { setView(next); setMoreOpen(false); window.history.replaceState(null, '', `#${next}`); window.scrollTo({ top: 0, behavior: 'smooth' }); }, []);
  const navigate = useCallback((next: View) => {
    if (view === 'groups' && next !== 'groups' && noteDraft) {
      draftOpenerRef.current = document.activeElement instanceof HTMLElement ? document.activeElement : null;
      setPendingView(next); setMoreOpen(false);
      return;
    }
    navigateNow(next);
  }, [view, noteDraft, navigateNow]);
  const closeCharacter = useCallback(() => setCharacter(null), []);
  const notify = useCallback((message: string, isError = false) => setToast({ message, error: isError }), []);
  const openCollection = useCallback(() => navigate('collection'), [navigate]);
  useTelegramCollectionButton(Boolean(profile) && !loading && view === 'home' && !character && !moreOpen && !pendingView, collectionButtonEmoji, openCollection);

  useEffect(() => {
    let active = true;
    const telegram = window.Telegram?.WebApp;
    telegram?.ready?.(); telegram?.expand?.();
    async function boot() {
      try {
        const status = await api.get<{ preview: boolean; collection_button_emoji?: CollectionButtonEmoji }>('/bootstrap');
        const user = await startSession(telegram?.initData || '');
        if (active) { setPreview(status.preview); setCollectionButtonEmoji(status.collection_button_emoji || fallbackCollectionButtonEmoji); setProfile(user); setError(''); }
      } catch (err) {
        if (active) setError(err instanceof Error ? err.message : 'Could not connect to SUMMON.');
      } finally { if (active) setLoading(false); }
    }
    void boot();
    return () => { active = false; };
  }, []);
  useEffect(() => { if (!toast) return; const timer = window.setTimeout(() => setToast(null), 4000); return () => window.clearTimeout(timer); }, [toast]);
  useEffect(() => { if (pendingView) draftConfirmRef.current?.focus(); }, [pendingView]);
  async function refreshProfile() { try { setProfile(await api.get<Profile>('/me')); } catch (err) { notify(err instanceof Error ? err.message : 'Could not refresh your profile.', true); } }
  async function daily() {
    if (preview || dailyBusy) return;
    setDailyBusy(true);
    try { const reward = await api.post<{ points: number; xp: number }>('/daily'); notify(`Daily reward claimed: +${reward.points} points and +${reward.xp} XP.`); await refreshProfile(); refresh(); }
    catch (err) { notify(err instanceof Error ? err.message : 'Could not claim your reward.', true); } finally { setDailyBusy(false); }
  }
  async function favorite(item: Character) {
    if (preview) return;
    try { await api.put(`/characters/${encodeURIComponent(item.id)}/favorite`, { value: !item.favorite }); notify(item.favorite ? 'Removed from favorites.' : 'Added to favorites.'); refresh(); }
    catch (err) { notify(err instanceof Error ? err.message : 'Could not update favorites.', true); }
  }
  async function logout() {
    try { await api.post('/auth/logout'); setProfile(null); setError('Signed out. Reopen SUMMON from Telegram to start a new session.'); }
    catch (err) { notify(err instanceof Error ? err.message : 'Could not sign out.', true); }
  }

  if (loading) return <div className="gate"><Brand /><div className="gate-loading"><span className="loader" /><p>Finding your universe…</p></div></div>;
  if (!profile) return <div className="gate"><Brand /><div className="gate-card"><span className="gate-icon"><Sparkles size={25} /></span><span className="eyebrow">COLLECT · DISCOVER · SUMMON</span><h1>Your story starts here.</h1><p>{error || 'Open SUMMON from your Telegram bot to continue.'}</p>{error && !(error.toLowerCase().includes('telegram') || error.toLowerCase().includes('signed out')) && <button className="button button-primary" onClick={() => window.location.reload()}>Try again</button>}<small>For your security, sign-in is verified by Telegram.</small></div></div>;

  const entries = profile.is_operator ? [...menu, { view: 'admin' as View, label: 'Catalogue admin', icon: ShieldCheck, group: 'ACCOUNT' }] : menu;
  const active = entries.find(item => item.view === view) || entries[0];
  const page = view === 'home' ? <Home profile={profile} preview={preview} revision={revision} navigate={navigate} openCharacter={setCharacter} favorite={favorite} daily={daily} dailyBusy={dailyBusy} />
    : view === 'collection' || view === 'catalog' ? <Catalog key={view} mode={view} preview={preview} revision={revision} onOpen={setCharacter} onFavorite={favorite} onDiscover={() => navigate('catalog')} />
    : view === 'trades' ? <Trading profile={profile} preview={preview} revision={revision} refresh={refresh} notify={notify} />
    : view === 'progress' ? <Progress preview={preview} revision={revision} daily={daily} dailyBusy={dailyBusy} />
    : view === 'leaderboard' ? <Leaderboard revision={revision} />
    : view === 'groups' ? <Groups preview={preview} revision={revision} refresh={refresh} notify={notify} onNoteDraftChange={setNoteDraft} />
    : view === 'profile' ? <ProfilePage profile={profile} navigate={navigate} />
    : view === 'admin' && profile.is_operator ? <Admin notify={notify} refresh={refresh} />
    : view === 'settings' ? <div className="page-content"><div className="page-heading"><span className="eyebrow">MAKE IT YOURS</span><h1>Settings.</h1><p>A few little preferences for your SUMMON experience.</p></div><div className="page-card group-panel"><div className="panel-title"><div><span className="eyebrow">APPEARANCE</span><h3>Choose your theme</h3><p>Follow Telegram as it changes, or choose a theme for this device.</p></div></div><div className="segment theme-segment"><button className={preference === 'telegram' ? 'active' : ''} aria-pressed={preference === 'telegram'} onClick={() => chooseTheme('telegram')}>Follow Telegram</button><button className={preference === 'dark' ? 'active' : ''} aria-pressed={preference === 'dark'} onClick={() => chooseTheme('dark')}><Moon size={15} /> Dark</button><button className={preference === 'light' ? 'active' : ''} aria-pressed={preference === 'light'} onClick={() => chooseTheme('light')}><Sun size={15} /> Light</button></div></div><div className="page-card group-panel"><span className="eyebrow">YOUR SESSION</span><h3>Signed in with Telegram</h3><p className="muted-small">Your identity is verified on the server. Reopen the Mini App in Telegram when your session expires.</p><button className="button button-subtle" disabled={preview} onClick={logout}><LogOut size={15} /> Sign out</button></div></div>
    : <div className="page-content"><EmptyState title="Not available" text="You don't have access to this area." /></div>;

  return <div className="app-shell"><a className="skip-link" href="#main-content">Skip to main content</a><aside className="sidebar"><Brand /><div className="sidebar-divider" />
    <nav aria-label="Main navigation">{['YOUR SPACE', 'COMMUNITY', 'ACCOUNT'].map(group => <div className="nav-group" key={group}><span className="nav-label">{group}</span>{entries.filter(item => item.group === group).map(item => <button key={item.view} className={`nav-item ${view === item.view ? 'active' : ''}`} onClick={() => navigate(item.view)} aria-current={view === item.view ? 'page' : undefined}><item.icon size={18} strokeWidth={1.7} /><span>{item.label}</span>{view === item.view && <span className="nav-active-dot" />}</button>)}</div>)}</nav>
    <div className="sidebar-foot"><div className="sidebar-foot-icon"><Sparkles size={16} /></div><div><strong>A universe to call yours.</strong><small>COLLECT. DISCOVER. SUMMON.</small></div></div>
  </aside>
  <main className="main-shell" id="main-content" tabIndex={-1}><header className="topbar"><div className="mobile-brand"><Brand compact /></div><div className="breadcrumbs"><span>YOUR SPACE</span><span>/</span><strong>{active.label}</strong></div><div className="topbar-actions"><button className="icon-button top-search" onClick={() => navigate('catalog')} aria-label="Search characters"><Search size={19} /></button><button className="icon-button" onClick={toggleTheme} aria-label={`Switch to ${theme === 'dark' ? 'light' : 'dark'} theme`}>{theme === 'dark' ? <Sun size={18} /> : <Moon size={18} />}</button><span className="topbar-divider" /><button className="user-chip" onClick={() => navigate('profile')} aria-label="Open your profile"><span className="user-avatar">{profile.first_name.charAt(0).toUpperCase()}</span><span><strong>{profile.first_name}</strong><small>LEVEL {profile.level} COLLECTOR</small></span><ChevronDown size={14} /></button></div></header>
    {preview && <div className="preview-banner"><Sparkles size={14} /> Read-only preview <span>·</span> Sample characters and group data</div>}
    {pendingView && <div ref={draftConfirmRef} tabIndex={-1} className="warning-confirm app-draft-warning" role="region" aria-label="Discard note draft"><h4>Leave your note draft?</h4><p>Leaving will discard the draft. If a save is already in progress, it may still complete; review the notes before retrying.</p><div className="warning-confirm-actions"><button className="button button-subtle" type="button" onClick={() => { setPendingView(null); draftOpenerRef.current?.focus(); }}>Keep editing</button><button className="button button-primary" type="button" onClick={() => { const next = pendingView; setPendingView(null); setNoteDraft(false); navigateNow(next); }}>Discard and continue</button></div></div>}
    {page}<footer className="app-footer"><span>✦ &nbsp; A LITTLE UNIVERSE FOR COLLECTORS</span><span>SUMMON © {new Date().getFullYear()} &nbsp; · &nbsp; COLLECT. DISCOVER. SUMMON.</span></footer>
  </main>
  <nav className="mobile-nav" aria-label="Mobile navigation">{([['home', HomeIcon, 'Home'], ['collection', BookOpen, 'Collection'], ['catalog', Compass, 'Discover'], ['groups', Users, 'Groups']] as const).map(([name, IconComponent, label]) => <button key={name} className={view === name ? 'active' : ''} onClick={() => navigate(name)}><IconComponent size={19} /><span>{label}</span></button>)}<button className={moreOpen ? 'active' : ''} onClick={() => setMoreOpen(!moreOpen)} aria-expanded={moreOpen} aria-label="More navigation">{moreOpen ? <X size={19} /> : <Menu size={19} />}<span>More</span></button></nav>
  {moreOpen && <div className="mobile-more"><div className="mobile-more-title"><span className="eyebrow">EXPLORE SUMMON</span><button className="icon-button" onClick={() => setMoreOpen(false)} aria-label="Close menu"><X size={18} /></button></div>{entries.filter(item => !['home', 'collection', 'catalog', 'groups'].includes(item.view)).map(item => <button key={item.view} onClick={() => navigate(item.view)}><item.icon size={18} /> {item.label}</button>)}</div>}
  {character && <CharacterDetail id={character} preview={preview} onClose={closeCharacter} onChanged={refresh} notify={notify} />}
  {toast && <div className={`toast ${toast.error ? 'toast-error' : ''}`} role="status">{toast.error ? '!' : '✦'} <span>{toast.message}</span><button onClick={() => setToast(null)} aria-label="Dismiss notification"><X size={15} /></button></div>}
  </div>;
}
