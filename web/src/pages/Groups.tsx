import { Activity, ArrowRight, Check, CircleAlert, Link2, MessageCircle, Settings2, ShieldCheck, Sparkles, Users } from 'lucide-react';
import { useCallback, useEffect, useRef, useState } from 'react';
import { ApiError, api } from '../api';
import { EmptyState, SectionTitle } from '../components';
import { useRemote } from '../hooks';
import type { Group, GroupSettings } from '../types';
import WarningReview from './WarningReview';
import GroupNotes from './GroupNotes';
import GroupAutoReplies from './GroupAutoReplies';
import GroupSummonInsights from './GroupSummonInsights';

type Tab = 'overview' | 'summon-insights' | 'protection' | 'community' | 'moderation' | 'notes' | 'auto-replies';
interface Props { preview: boolean; revision: number; refresh: () => void; notify: (message: string, error?: boolean) => void; onNoteDraftChange?: (hasDraft: boolean, kind?: 'note' | 'auto-reply') => void; onMutationChange?: (inFlight: boolean) => void }
type PendingNavigation = { tab: Tab } | { group: number };

function Toggle({ label, hint, checked, onChange, disabled }: { label: string; hint: string; checked: boolean; onChange: (value: boolean) => void; disabled?: boolean }) {
  return <label className="setting-row"><span><strong>{label}</strong><small>{hint}</small></span><input type="checkbox" className="switch-input" checked={checked} disabled={disabled} onChange={event => onChange(event.target.checked)} /><span className="switch" aria-hidden="true" /></label>;
}

export default function Groups({ preview, revision, refresh, notify, onNoteDraftChange, onMutationChange }: Props) {
  const { data: groups, loading, error } = useRemote<Group[]>('/groups', revision);
  const [selected, setSelected] = useState<number | null>(null);
  const selectedRef = useRef<number | null>(null);
  const [tab, setTab] = useState<Tab>('overview');
  const [noteDraft, setNoteDraft] = useState(false);
  const [pendingNavigation, setPendingNavigation] = useState<PendingNavigation | null>(null);
  const navigationConfirmRef = useRef<HTMLDivElement>(null);
  const navigationOpenerRef = useRef<HTMLElement | null>(null);
  // Refresh audit activity after leaving the editor, not while it is recovering
  // a confirmed save whose follow-up list request may have failed.
  const pendingAutoRefresh = useRef(false);
  const markAutoRefresh = useCallback(() => { pendingAutoRefresh.current = true; }, []);
  function refreshAutoActivity() { if (pendingAutoRefresh.current) { pendingAutoRefresh.current = false; refresh(); } }
  const reportNoteDraft = useCallback((value: boolean) => { setNoteDraft(value); onNoteDraftChange?.(value, 'note'); }, [onNoteDraftChange]);
  const reportReplyDraft = useCallback((value: boolean) => { setNoteDraft(value); onNoteDraftChange?.(value, 'auto-reply'); }, [onNoteDraftChange]);
  const [settings, setSettings] = useState<GroupSettings | null>(null);
  const [settingsSource, setSettingsSource] = useState<Group | null>(null);
  const [keywords, setKeywords] = useState('');
  const [target, setTarget] = useState('');
  const [action, setAction] = useState('warn');
  const [reason, setReason] = useState('');
  const [duration, setDuration] = useState('');
  const [uncertainModeration, setUncertainModeration] = useState<{ groupId: number; targetId: number; action: string } | null>(null);
  const uncertainModerationRef = useRef(false);
  const [busy, setBusy] = useState(false);
  const mutationInFlight = useRef(false);
  function markBusy(value: boolean) { mutationInFlight.current = value; onMutationChange?.(value); setBusy(value); }
  useEffect(() => {
    if (!busy) return;
    const warn = (event: BeforeUnloadEvent) => { event.preventDefault(); event.returnValue = ''; };
    window.addEventListener('beforeunload', warn);
    return () => window.removeEventListener('beforeunload', warn);
  }, [busy]);
  useEffect(() => { if (groups?.length && !groups.some(group => group.id === selected)) { selectedRef.current = groups[0].id; setSelected(groups[0].id); } }, [groups, selected]);
  const { data: group, loading: groupLoading, error: groupError } = useRemote<Group>(selected !== null ? `/groups/${selected}` : null, revision);
  useEffect(() => { if (group) { setSettings({ ...group.settings }); setKeywords(group.settings.blocked_keywords.join(', ')); setSettingsSource(group); } }, [group]);
  const groupReady = Boolean(group && selected === group.id && settings && settingsSource === group);
  useEffect(() => { if (pendingNavigation) navigationConfirmRef.current?.focus(); }, [pendingNavigation]);
  function changeTab(next: Tab, opener: HTMLElement) {
    if (mutationInFlight.current || next === tab) return;
    if ((tab === 'notes' || tab === 'auto-replies') && noteDraft) { navigationOpenerRef.current = opener; setPendingNavigation({ tab: next }); }
    else { setTab(next); if (tab === 'auto-replies') refreshAutoActivity(); }
  }
  function changeGroup(next: number, opener: HTMLElement) {
    if (mutationInFlight.current || next === selected) return;
    if ((tab === 'notes' || tab === 'auto-replies') && noteDraft) { navigationOpenerRef.current = opener; setPendingNavigation({ group: next }); }
    else { selectedRef.current = next; setSelected(next); setTab('overview'); if (tab === 'auto-replies') refreshAutoActivity(); }
  }
  function continueNavigation() {
    if (!pendingNavigation) return;
    const next = pendingNavigation;
    setPendingNavigation(null);
    if (tab === 'auto-replies') reportReplyDraft(false); else reportNoteDraft(false);
    if ('tab' in next) setTab(next.tab);
    else { selectedRef.current = next.group; setSelected(next.group); setTab('overview'); }
    if (tab === 'auto-replies') refreshAutoActivity();
  }
  function set<K extends keyof GroupSettings>(key: K, value: GroupSettings[K]) { setSettings(current => current ? { ...current, [key]: value } : current); }
  async function save(part: 'overview' | 'protection' | 'community') {
    if (!group || !settings || !groupReady || selectedRef.current !== group.id || preview || mutationInFlight.current) return;
    const patch: Partial<GroupSettings> = part === 'overview' ? {
      spawn_enabled: settings.spawn_enabled, spawn_every: settings.spawn_every,
      spawn_cooldown_seconds: settings.spawn_cooldown_seconds, spawn_lifetime_seconds: settings.spawn_lifetime_seconds,
    } : part === 'protection' ? {
      link_filter: settings.link_filter, flood_enabled: settings.flood_enabled, flood_limit: settings.flood_limit,
      flood_window_seconds: settings.flood_window_seconds, duplicate_enabled: settings.duplicate_enabled,
      duplicate_limit: settings.duplicate_limit, mention_limit: settings.mention_limit,
      blocked_keywords: keywords.split(',').map(word => word.trim()).filter(Boolean),
    } : { welcome_enabled: settings.welcome_enabled, welcome_text: settings.welcome_text, goodbye_enabled: settings.goodbye_enabled, goodbye_text: settings.goodbye_text, rules: settings.rules };
    markBusy(true);
    try { await api.patch(`/groups/${group.id}/settings`, patch); notify('Group settings saved.'); refresh(); }
    catch (err) { notify(err instanceof Error ? err.message : 'Could not save settings.', true); } finally { markBusy(false); }
  }
  async function moderate(event: React.FormEvent) {
    event.preventDefault();
    if (!group || !groupReady || selectedRef.current !== group.id || preview || mutationInFlight.current || uncertainModerationRef.current) return;
    const id = Number(target);
    if (!Number.isSafeInteger(id) || id <= 0) { notify('Enter a valid Telegram user ID.', true); return; }
    const seconds = duration ? Number(duration) * 60 : undefined;
    if (seconds !== undefined && (!Number.isInteger(seconds) || seconds < 60 || seconds > 366 * 86400)) { notify('Duration must be between 1 minute and 1 year.', true); return; }
    markBusy(true);
    try { await api.post(`/groups/${group.id}/moderation`, { target_id: id, action, reason, ...(seconds && ['ban', 'mute'].includes(action) ? { duration_seconds: seconds } : {}) });
      notify(action === 'warn' ? 'Warning recorded; no automatic penalty was applied.' : 'Telegram confirmed the moderation action.'); setTarget(''); setReason(''); setDuration(''); refresh();
    } catch (err) {
      if (err instanceof ApiError && [401, 403, 422, 429].includes(err.status)) notify(err.message, true);
      else {
        uncertainModerationRef.current = true;
        setUncertainModeration({ groupId: group.id, targetId: id, action });
        notify('The action may have completed; review the member and activity before retrying.', true);
      }
    } finally { markBusy(false); }
  }
  return <div className="page-content"><div className="page-heading"><span className="eyebrow">GREAT COMMUNITIES GROW TOGETHER</span><h1>Your groups.</h1><p>A calmer, safer place for every collection and conversation.</p></div>
    {loading ? <div className="loading-line">Loading your groups…</div> : error ? <EmptyState title="Couldn't load your groups" text={error} /> : !groups?.length ? <EmptyState title="No groups connected yet" text="Add SUMMON to your group, then use /manage there as a group administrator. Your current Telegram permissions are checked every time." /> : <>
      <div className="group-select-row"><label>YOUR GROUP <select value={selected ?? ''} disabled={busy} onChange={event => changeGroup(Number(event.target.value), event.currentTarget)}>{groups.map(item => <option value={item.id} key={item.id}>{item.title}</option>)}</select></label>{preview && <span className="sample-label">SAMPLE GROUP · READ ONLY</span>}</div>
      {busy && <p className="muted-small" role="status">A group change is in progress. Please wait before leaving.</p>}
      {groupLoading || (group && !groupReady) ? <div className="loading-line">Loading group…</div> : groupError ? <EmptyState title="Couldn't load this group" text={groupError} /> : group && settings && groupReady && <>
        <div className="group-header"><div className="group-avatar"><Users size={23} /></div><div><span className="eyebrow">GROUP MANAGEMENT</span><h2>{group.title}</h2><p>Permission-aware controls for your community.</p></div><span className="group-badge"><ShieldCheck size={14} /> ADMIN ACCESS</span></div>
        {pendingNavigation && <div ref={navigationConfirmRef} tabIndex={-1} className="warning-confirm" role="region" aria-label={tab === 'auto-replies' ? 'Discard auto-reply draft' : 'Discard note draft'}><h4>Leave your {tab === 'auto-replies' ? 'auto-reply' : 'note'} draft?</h4><p>Leaving will discard the draft. If a save is already in progress, it may still complete; review the {tab === 'auto-replies' ? 'auto replies' : 'notes'} before retrying.</p><div className="warning-confirm-actions"><button className="button button-subtle" type="button" onClick={() => { setPendingNavigation(null); navigationOpenerRef.current?.focus(); }}>Keep editing</button><button className="button button-primary" type="button" onClick={continueNavigation}>Discard and continue</button></div></div>}
        <div className="group-tabs" role="tablist" aria-label="Group management sections">
          <button role="tab" disabled={busy} aria-selected={tab === 'overview'} onClick={event => changeTab('overview', event.currentTarget)}>Overview</button>
          <button role="tab" disabled={busy} aria-selected={tab === 'summon-insights'} onClick={event => changeTab('summon-insights', event.currentTarget)}>Summon insights</button>
          {group.permissions.can_restrict_members && <button role="tab" disabled={busy} aria-selected={tab === 'protection'} onClick={event => changeTab('protection', event.currentTarget)}>Protection</button>}
          {group.permissions.can_change_info && <button role="tab" disabled={busy} aria-selected={tab === 'community'} onClick={event => changeTab('community', event.currentTarget)}>Welcome & rules</button>}
          {group.permissions.can_change_info && <button role="tab" disabled={busy} aria-selected={tab === 'notes'} onClick={event => changeTab('notes', event.currentTarget)}>Notes</button>}
          {group.permissions.can_change_info && <button role="tab" disabled={busy} aria-selected={tab === 'auto-replies'} onClick={event => changeTab('auto-replies', event.currentTarget)}>Auto replies</button>}
          {group.permissions.can_restrict_members && <button role="tab" disabled={busy} aria-selected={tab === 'moderation'} onClick={event => changeTab('moderation', event.currentTarget)}>Moderation</button>}
        </div>
        {tab === 'overview' && <div className="group-layout"><div><div className="group-stat-grid"><div className="page-card"><MessageCircle size={18} /><span>Messages counted</span><strong>{group.total_messages.toLocaleString()}</strong></div><div className="page-card"><Sparkles size={18} /><span>Character summons</span><strong>{settings.spawn_enabled ? 'Enabled' : 'Off'}</strong></div><div className="page-card"><Activity size={18} /><span>Summon frequency</span><strong>{settings.spawn_every} messages</strong></div></div>
          <div className="page-card group-panel"><div className="panel-title"><div><span className="eyebrow">COLLECTION SETTINGS</span><h3>Character summons</h3><p>Keep appearances fun without overwhelming the conversation.</p></div><Sparkles size={19} /></div>
            <Toggle label="Automatic summons" hint="Characters can appear as members chat." checked={settings.spawn_enabled} onChange={value => set('spawn_enabled', value)} disabled={preview || busy || !group.permissions.can_change_info} />
            <div className="form-grid group-form"><label>Messages per summon<input type="number" min="20" max="10000" value={settings.spawn_every} disabled={preview || busy || !group.permissions.can_change_info} onChange={event => set('spawn_every', Number(event.target.value))} /></label><label>Minimum cooldown (seconds)<input type="number" min="60" max="86400" value={settings.spawn_cooldown_seconds} disabled={preview || busy || !group.permissions.can_change_info} onChange={event => set('spawn_cooldown_seconds', Number(event.target.value))} /></label></div>
            {group.permissions.can_change_info && <button className="button button-primary" disabled={preview || busy} onClick={() => save('overview')}>Save summon settings <ArrowRight size={15} /></button>}
          </div></div><div className="page-card activity-panel"><div className="panel-title"><div><span className="eyebrow">THE LATEST</span><h3>Recent activity</h3></div><Activity size={18} /></div>{group.recent_activity?.length ? group.recent_activity.map((item, index) => <div className="activity-row" key={`${item.created_at}-${index}`}><span className="activity-dot" /><div><strong>{item.kind === 'moderation' ? item.action === 'warning_revoked' ? 'Warning revoked' : `${item.action || 'Moderation'} recorded` : item.kind === 'spam_removed' ? 'Spam message removed' : item.kind === 'settings_changed' ? 'Settings updated' : item.kind === 'note_changed' ? `Note ${item.action || 'changed'}` : item.kind === 'auto_reply_changed' ? `Auto reply ${item.action || 'changed'}` : item.kind.replaceAll('_', ' ')}</strong><small>{item.reason || new Date(item.created_at).toLocaleDateString()}</small>{item.warning_id && <small>Warning {item.warning_id}</small>}{item.note_name && <small>Note {item.note_name}</small>}{item.reply_name && <small>Auto reply {item.reply_name}</small>}</div></div>) : <p className="muted-small">No recent activity yet.</p>}</div></div>}
        {tab === 'summon-insights' && <GroupSummonInsights key={group.id} groupId={group.id} revision={revision} />}
        {tab === 'protection' && group.permissions.can_restrict_members && <div className="page-card group-panel"><div className="panel-title"><div><span className="eyebrow">KEEP THE CONVERSATION HEALTHY</span><h3>Protection settings</h3><p>Suspected messages are removed only when the bot has permission. Administrators are exempt.</p></div><ShieldCheck size={20} /></div>
          <Toggle label="Link filter" hint="Remove messages containing web or Telegram links." checked={settings.link_filter} onChange={value => set('link_filter', value)} disabled={preview || busy} />
          <Toggle label="Flood control" hint="Limit rapid messages from the same member." checked={settings.flood_enabled} onChange={value => set('flood_enabled', value)} disabled={preview || busy} />
          <Toggle label="Duplicate protection" hint="Detect repeated messages in a short window." checked={settings.duplicate_enabled} onChange={value => set('duplicate_enabled', value)} disabled={preview || busy} />
          <div className="form-grid group-form"><label>Flood limit<input type="number" min="3" max="30" value={settings.flood_limit} disabled={preview || busy} onChange={event => set('flood_limit', Number(event.target.value))} /></label><label>Flood window (seconds)<input type="number" min="5" max="120" value={settings.flood_window_seconds} disabled={preview || busy} onChange={event => set('flood_window_seconds', Number(event.target.value))} /></label><label>Duplicate limit<input type="number" min="2" max="10" value={settings.duplicate_limit} disabled={preview || busy} onChange={event => set('duplicate_limit', Number(event.target.value))} /></label><label>Mention limit (0 = off)<input type="number" min="0" max="20" value={settings.mention_limit} disabled={preview || busy} onChange={event => set('mention_limit', Number(event.target.value))} /></label><label className="wide-field">Blocked keywords, comma-separated<input value={keywords} maxLength={1000} disabled={preview || busy} onChange={event => setKeywords(event.target.value)} placeholder="Add words or phrases" /></label></div>
          <button className="button button-primary" disabled={preview || busy} onClick={() => save('protection')}>Save protection settings <ArrowRight size={15} /></button></div>}
        {tab === 'community' && group.permissions.can_change_info && <div className="page-card group-panel"><div className="panel-title"><div><span className="eyebrow">MAKE EVERYONE FEEL AT HOME</span><h3>Welcome & rules</h3><p>Set a warm welcome and a clear guide for your members.</p></div><MessageCircle size={20} /></div>
          <Toggle label="Welcome new members" hint="Send your welcome message when members join." checked={settings.welcome_enabled} onChange={value => set('welcome_enabled', value)} disabled={preview || busy} />
          <Toggle label="Say goodbye to members" hint="Send a limited notice when someone leaves or is removed." checked={settings.goodbye_enabled} onChange={value => set('goodbye_enabled', value)} disabled={preview || busy} />
          <div className="form-grid group-form"><label className="wide-field">Welcome message<textarea maxLength={1000} value={settings.welcome_text} disabled={preview || busy} onChange={event => set('welcome_text', event.target.value)} /></label><label className="wide-field">Goodbye message<textarea maxLength={1000} value={settings.goodbye_text} disabled={preview || busy} onChange={event => set('goodbye_text', event.target.value)} /></label><label className="wide-field">Group rules<textarea maxLength={3000} value={settings.rules} disabled={preview || busy} onChange={event => set('rules', event.target.value)} /></label></div>
          <button className="button button-primary" disabled={preview || busy} onClick={() => save('community')}>Save community settings <ArrowRight size={15} /></button></div>}
        {tab === 'notes' && group.permissions.can_change_info && <GroupNotes key={group.id} groupId={group.id} preview={preview} notify={notify} onChanged={refresh} onDraftChange={reportNoteDraft} />}
        {tab === 'auto-replies' && group.permissions.can_change_info && <GroupAutoReplies key={group.id} groupId={group.id} preview={preview} notify={notify} onChanged={markAutoRefresh} onDraftChange={reportReplyDraft} />}
        {tab === 'moderation' && group.permissions.can_restrict_members && <><form className="page-card group-panel" onSubmit={moderate}><div className="panel-title"><div><span className="eyebrow">ADMINISTRATOR TOOLS</span><h3>Moderate a member</h3><p>SUMMON checks your current Telegram rights and the bot's permissions before every action.</p></div><CircleAlert size={20} /></div>
          <div className="form-grid group-form"><label>Telegram user ID<input inputMode="numeric" value={target} disabled={preview || busy} onChange={event => setTarget(event.target.value)} required placeholder="Member's numeric ID" /></label><label>Action<select value={action} disabled={preview || busy} onChange={event => setAction(event.target.value)}><option value="warn">Warn</option><option value="mute" disabled>Mute (use Telegram)</option><option value="unmute" disabled>Unmute (use Telegram)</option><option value="ban">Ban</option><option value="unban" disabled>Unban (use Telegram)</option><option value="kick" disabled>Kick (use Telegram)</option></select></label><label className="wide-field">Reason<input maxLength={500} value={reason} disabled={preview || busy} onChange={event => setReason(event.target.value)} placeholder="Optional reason for the action log" /></label>{['ban', 'mute'].includes(action) && <label>Duration in minutes (optional)<input type="number" min="1" max="527040" value={duration} disabled={preview || busy} onChange={event => setDuration(event.target.value)} placeholder="Leave blank for indefinite" /></label>}</div>
          <p className="field-hint">Warning records do not trigger an automatic penalty. Admins and the bot cannot be targeted. Use Telegram directly to mute, unmute, unban or kick; these actions are unavailable here.</p>
          {uncertainModeration && <div className="warning-confirm" role="alert"><h4>Check before trying again</h4><p>The {uncertainModeration.action} for member {uncertainModeration.targetId} in group {uncertainModeration.groupId} may have completed. Review the warning record or Telegram membership and group activity first.</p><button className="button button-subtle" type="button" onClick={() => { uncertainModerationRef.current = false; setUncertainModeration(null); }}>I've reviewed the action</button></div>}
          <button className="button button-primary" type="submit" disabled={preview || busy || Boolean(uncertainModeration)}><Check size={15} /> Apply action</button></form><WarningReview key={group.id} groupId={group.id} preview={preview} notify={notify} /></>}
      </>}
    </>}
  </div>;
}
