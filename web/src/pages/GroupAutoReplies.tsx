import { MessageCircleReply, Plus, Trash2 } from 'lucide-react';
import { useEffect, useRef, useState } from 'react';
import { ApiError, api } from '../api';
import type { GroupAutoReply } from '../types';

interface Props {
  groupId: number;
  preview: boolean;
  notify: (message: string, error?: boolean) => void;
  onChanged?: () => void;
  onDraftChange?: (hasDraft: boolean) => void;
}

const unsupported = /[\p{Cc}\p{Cf}\p{Cs}]/u;
const allowedReplyControls = new Set(['\n', '\t', '\u200c', '\u200d']);

/** These are public, opt-in exact matches, not private or on-demand notes. */
export default function GroupAutoReplies({ groupId, preview, notify, onChanged, onDraftChange }: Props) {
  const [rules, setRules] = useState<GroupAutoReply[] | null>(null);
  const [selectedName, setSelectedName] = useState<string | null>(null);
  const [name, setName] = useState('');
  const [trigger, setTrigger] = useState('');
  const [response, setResponse] = useState('');
  const [enabled, setEnabled] = useState(false);
  const [loading, setLoading] = useState(true);
  const [busy, setBusy] = useState(false);
  const [needsReload, setNeedsReload] = useState(false);
  const [error, setError] = useState('');
  const [confirming, setConfirming] = useState(false);
  const version = useRef(0);
  const mutationInFlight = useRef<object | null>(null);
  const confirmationRef = useRef<HTMLDivElement>(null);
  const openerRef = useRef<HTMLButtonElement>(null);
  const selected = rules?.find(rule => rule.name === selectedName) ?? null;
  const dirty = selected
    ? trigger !== selected.trigger || response !== selected.response || enabled !== selected.enabled
    : Boolean(name || trigger || response || enabled);
  const length = Array.from(response).length;

  async function load(afterChange?: 'save' | 'delete') {
    const current = ++version.current;
    setLoading(true); setError('');
    try {
      const data = await api.get<GroupAutoReply[]>(`/groups/${groupId}/auto-replies`);
      if (current !== version.current) return;
      const first = data[0] ?? null;
      setRules(data); setSelectedName(first?.name ?? null);
      setName(first?.name ?? ''); setTrigger(first?.trigger ?? '');
      setResponse(first?.response ?? ''); setEnabled(first?.enabled ?? false);
      setNeedsReload(false); setConfirming(false);
    } catch (err) {
      if (current === version.current) {
        if (afterChange) {
          setNeedsReload(true);
          setError(afterChange === 'save'
            ? 'Auto reply saved, but the list could not reload. Copy your draft, then reload before continuing.'
            : 'Auto reply deleted, but the list could not reload. Reload before continuing.');
        } else setError(err instanceof Error ? err.message : 'Could not load auto replies.');
      }
    } finally {
      if (current === version.current) setLoading(false);
    }
  }

  useEffect(() => {
    setRules(null); setSelectedName(null); setName(''); setTrigger(''); setResponse(''); setEnabled(false);
    setNeedsReload(false); setConfirming(false); setBusy(false);
    mutationInFlight.current = null;
    void load();
    return () => { version.current += 1; };
    // Group identity owns this fetch; subsequent reads are explicit.
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [groupId]);
  useEffect(() => { if (confirming) confirmationRef.current?.focus(); }, [confirming]);
  useEffect(() => { onDraftChange?.(dirty || busy); }, [dirty, busy, onDraftChange]);
  useEffect(() => () => { onDraftChange?.(false); }, [onDraftChange]);
  useEffect(() => {
    if (!dirty && !busy) return;
    function warn(event: BeforeUnloadEvent) { event.preventDefault(); event.returnValue = ''; }
    window.addEventListener('beforeunload', warn);
    return () => window.removeEventListener('beforeunload', warn);
  }, [dirty, busy]);

  function select(rule: GroupAutoReply | null) {
    if (dirty) { setError('Save or discard your draft before switching auto replies.'); return; }
    setError(''); setConfirming(false); setSelectedName(rule?.name ?? null);
    setName(rule?.name ?? ''); setTrigger(rule?.trigger ?? '');
    setResponse(rule?.response ?? ''); setEnabled(rule?.enabled ?? false);
  }
  function discardDraft() {
    setName(selected?.name ?? ''); setTrigger(selected?.trigger ?? '');
    setResponse(selected?.response ?? ''); setEnabled(selected?.enabled ?? false);
    setConfirming(false); setError('');
  }

  async function save(event: React.FormEvent) {
    event.preventDefault();
    if (preview || busy || loading || confirming || needsReload || mutationInFlight.current || !rules) return;
    if (!/^[a-z0-9][a-z0-9_-]{0,31}$/.test(name)) {
      setError('Names use 1–32 lowercase letters, numbers, underscores or hyphens.'); return;
    }
    // The server applies full Unicode casefolding and is authoritative for 2–64 normalized code points.
    if (!trigger.trim() || Array.from(trigger).length > 256 || unsupported.test(trigger) || trigger.normalize('NFKC').trim().startsWith('/')) {
      setError('Check the trigger: use a short, single-line phrase, not a /command.'); return;
    }
    if (!response.trim() || length > 800 || Array.from(response).some(char => unsupported.test(char) && !allowedReplyControls.has(char))) {
      setError('Enter 1–800 reply characters without unsupported controls.'); return;
    }
    const current = ++version.current;
    const mutation = {};
    mutationInFlight.current = mutation;
    setBusy(true); setError('');
    try {
      await api.put(`/groups/${groupId}/auto-replies/${encodeURIComponent(name)}`, {
        trigger, response, enabled, expected_revision: selected?.revision ?? null,
      });
      if (current !== version.current) return;
      setBusy(false); notify('Auto reply saved.');
      void load('save'); onChanged?.();
    } catch (err) {
      if (current !== version.current) return;
      setBusy(false); setConfirming(false);
      if (err instanceof ApiError && err.status === 422) {
        setError('Check the trigger and reply. Triggers must be 2–64 normalized characters.');
      } else {
        setNeedsReload(true);
        setError(err instanceof ApiError && err.status === 409
          ? 'This rule changed, the trigger is already used, or the group is full. Copy your draft, then reload before trying again.'
          : 'Could not confirm the change. Copy your draft, then reload auto replies before trying again.');
      }
    } finally {
      if (mutationInFlight.current === mutation) mutationInFlight.current = null;
    }
  }

  async function remove() {
    if (!selected || preview || busy || loading || needsReload || dirty || mutationInFlight.current) return;
    const current = ++version.current;
    const mutation = {};
    mutationInFlight.current = mutation;
    setBusy(true); setError('');
    try {
      await api.post(`/groups/${groupId}/auto-replies/${encodeURIComponent(selected.name)}/delete`, { expected_revision: selected.revision });
      if (current !== version.current) return;
      setBusy(false); setConfirming(false); notify('Auto reply deleted.');
      void load('delete'); onChanged?.();
    } catch (err) {
      if (current !== version.current) return;
      setBusy(false); setNeedsReload(true); setConfirming(false);
      setError(err instanceof ApiError && err.status === 409
        ? 'This rule changed or was removed. Reload auto replies before trying again.'
        : 'Could not confirm the deletion. Reload auto replies and review before trying again.');
    } finally {
      if (mutationInFlight.current === mutation) mutationInFlight.current = null;
    }
  }

  return <section className="page-card group-panel group-auto-replies" aria-labelledby="auto-replies-title">
    <div className="panel-title"><div><span className="eyebrow">A LITTLE HELP, AUTOMATICALLY</span><h3 id="auto-replies-title">Auto replies</h3><p>Respond to an exact message after group protection has checked it.</p></div><MessageCircleReply size={20} /></div>
    <div className="notes-public-notice"><strong>Public in this group</strong><span>Triggers and replies are public and can be shared. Never include secrets or private information. New rules start disabled.</span></div>
    <p className="field-hint">Telegram must deliver ordinary group messages to the bot. Forwarded, edited, bot, anonymous, ephemeral and /command messages never trigger a reply. Rate limits can suppress replies.</p>
    {preview && <p className="field-hint">Sample data only · auto replies cannot be changed in this preview.</p>}
    {loading && <p className="muted-small" role="status">Loading auto replies…</p>}
    {error && <p className="notes-error" role="alert">{error}</p>}
    {needsReload && <button className="text-action notes-reload" type="button" disabled={busy || loading} onClick={() => void load()}>Reload auto replies (discard draft)</button>}
    {!loading && rules === null && <button className="button button-subtle" type="button" onClick={() => void load()}>Try again</button>}
    {rules && <div className="notes-layout">
      <aside className="notes-sidebar" aria-label="Saved auto replies">
        <div className="notes-list-heading"><span>RULES <small>{rules.length} / 20</small></span><button className="notes-new" type="button" aria-label="New auto reply" disabled={preview || busy || loading || confirming || needsReload || rules.length >= 20} onClick={() => select(null)}><Plus size={16} /> New</button></div>
        {rules.length ? <ul className="notes-list">{rules.map(rule => <li key={rule.name}><button type="button" aria-label={`${preview ? 'View' : 'Edit'} auto reply ${rule.name}`} aria-pressed={selectedName === rule.name} disabled={busy || loading || confirming || needsReload} onClick={() => select(rule)}><strong>{rule.name}</strong><small>{rule.enabled ? 'Enabled' : 'Disabled'} · {rule.trigger}</small></button></li>)}</ul> : <p className="muted-small">No auto replies yet. Create a disabled rule to get started.</p>}
        {rules.length >= 20 && <p className="muted-small">The 20-rule limit has been reached.</p>}
      </aside>
      <form className="notes-editor" onSubmit={save}>
        <div className="notes-editor-heading"><span className="eyebrow">{selected ? 'EDITING AUTO REPLY' : 'NEW AUTO REPLY'}</span>{selected && <strong>{selected.name}</strong>}</div>
        {!selected && <label>Rule name<input aria-label="Rule name" aria-describedby="reply-name-hint" value={name} maxLength={32} disabled={preview || busy || loading || confirming} readOnly={needsReload} onChange={event => setName(event.target.value.toLowerCase())} placeholder="e.g. greeting" autoComplete="off" /><small id="reply-name-hint">Lowercase letters, numbers, hyphens and underscores.</small></label>}
        <label>Exact trigger<input aria-label="Exact trigger" aria-describedby="reply-trigger-hint" value={trigger} maxLength={512} disabled={preview || busy || loading || confirming} readOnly={needsReload} onChange={event => setTrigger(event.target.value)} placeholder="e.g. hello everyone" autoComplete="off" /><small id="reply-trigger-hint">2–64 characters after server normalization. Case and spacing are normalized; no /commands.</small></label>
        <label>Reply text<textarea aria-label="Reply text" aria-describedby="reply-text-hint" value={response} maxLength={1600} disabled={preview || busy || loading || confirming} readOnly={needsReload} onChange={event => setResponse(event.target.value)} placeholder="Write a public reply…" /><small id="reply-text-hint">{length} / 800 characters · plain text, not markup</small></label>
        <label className="auto-reply-toggle"><input type="checkbox" aria-label="Enable this auto reply" checked={enabled} disabled={preview || busy || loading || confirming || needsReload} onChange={event => setEnabled(event.target.checked)} /><span><strong>Enable this auto reply</strong><small>Once saved, matching messages may receive this public reply.</small></span></label>
        <div className="notes-actions"><button className="button button-primary" type="submit" disabled={preview || busy || loading || confirming || needsReload || (selected === null && rules.length >= 20)}>{busy ? 'Saving…' : 'Save auto reply'}</button>{dirty && !preview && <button className="button button-subtle" type="button" disabled={busy || loading || needsReload} onClick={discardDraft}>Discard draft</button>}{selected && <button className="notes-delete" type="button" ref={openerRef} disabled={preview || busy || loading || needsReload || dirty} onClick={() => setConfirming(true)}><Trash2 size={15} /> Delete auto reply</button>}</div>
      </form>
    </div>}
    {confirming && selected && <div ref={confirmationRef} tabIndex={-1} className="warning-confirm" role="region" aria-label="Confirm auto-reply deletion"><h4>Delete this auto reply?</h4><p>Remove “{selected.name}” for everyone in this group? This cannot be undone in SUMMON. The removal is recorded without the trigger or reply text.</p><div className="warning-confirm-actions"><button className="button button-subtle" type="button" disabled={busy} onClick={() => { setConfirming(false); openerRef.current?.focus(); }}>Cancel</button><button className="button button-primary" type="button" disabled={busy} onClick={() => void remove()}>{busy ? 'Deleting…' : 'Confirm deletion'}</button></div></div>}
  </section>;
}
