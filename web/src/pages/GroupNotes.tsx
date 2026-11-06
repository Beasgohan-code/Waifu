import { FileText, Plus, Trash2 } from 'lucide-react';
import { useEffect, useRef, useState } from 'react';
import { ApiError, api } from '../api';
import type { GroupNote } from '../types';

interface Props {
  groupId: number;
  preview: boolean;
  notify: (message: string, error?: boolean) => void;
  onChanged?: () => void;
  onDraftChange?: (hasDraft: boolean) => void;
}

/** Notes are public in the group; revisions protect against accidental stale edits. */
export default function GroupNotes({ groupId, preview, notify, onChanged, onDraftChange }: Props) {
  const [notes, setNotes] = useState<GroupNote[] | null>(null);
  const [selectedName, setSelectedName] = useState<string | null>(null);
  const [name, setName] = useState('');
  const [content, setContent] = useState('');
  const [loading, setLoading] = useState(true);
  const [busy, setBusy] = useState(false);
  const [needsReload, setNeedsReload] = useState(false);
  const [error, setError] = useState('');
  const [confirming, setConfirming] = useState(false);
  const version = useRef(0);
  const mutationInFlight = useRef<object | null>(null);
  const confirmationRef = useRef<HTMLDivElement>(null);
  const openerRef = useRef<HTMLButtonElement>(null);
  const selected = notes?.find(note => note.name === selectedName) ?? null;
  const dirty = selected ? content !== selected.content : Boolean(name || content);
  const length = Array.from(content).length;

  async function load(afterChange?: 'save' | 'delete') {
    const current = ++version.current;
    setLoading(true); setError('');
    try {
      const data = await api.get<GroupNote[]>(`/groups/${groupId}/notes`);
      if (current !== version.current) return;
      const first = data[0] ?? null;
      setNotes(data); setSelectedName(first?.name ?? null);
      setName(first?.name ?? ''); setContent(first?.content ?? '');
      setNeedsReload(false); setConfirming(false);
    } catch (err) {
      if (current === version.current) {
        if (afterChange) {
          setNeedsReload(true);
          setError(afterChange === 'save'
            ? 'Note saved, but the list could not reload. Copy your draft, then reload notes before continuing.'
            : 'Note deleted, but the list could not reload. Reload notes before continuing.');
        } else setError(err instanceof Error ? err.message : 'Could not load notes.');
      }
    } finally {
      if (current === version.current) setLoading(false);
    }
  }

  useEffect(() => {
    setNotes(null); setSelectedName(null); setName(''); setContent('');
    setNeedsReload(false); setConfirming(false); setBusy(false);
    mutationInFlight.current = null;
    void load();
    return () => { version.current += 1; };
    // The group identity owns this fetch; later reads are initiated explicitly.
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

  function select(note: GroupNote | null) {
    if (dirty) {
      setError('Save or discard your draft before switching notes.');
      return;
    }
    setError(''); setConfirming(false); setSelectedName(note?.name ?? null);
    setName(note?.name ?? ''); setContent(note?.content ?? '');
  }

  function discardDraft() {
    setName(selected?.name ?? ''); setContent(selected?.content ?? '');
    setConfirming(false); setError('');
  }

  async function save(event: React.FormEvent) {
    event.preventDefault();
    if (preview || busy || loading || confirming || needsReload || mutationInFlight.current || !notes) return;
    if (!/^[a-z0-9][a-z0-9_-]{0,31}$/.test(name)) {
      setError('Names use 1–32 lowercase letters, numbers, underscores or hyphens.');
      return;
    }
    if (!content.trim() || length > 1200 || /[\x00-\x08\x0b-\x1f]/.test(content)) {
      setError('Enter 1–1200 characters without unsupported control characters.');
      return;
    }
    const current = ++version.current;
    const mutation = {};
    mutationInFlight.current = mutation;
    setBusy(true); setError('');
    try {
      await api.put(`/groups/${groupId}/notes/${encodeURIComponent(name)}`, {
        content, expected_revision: selected?.revision ?? null,
      });
      if (current !== version.current) return;
      setBusy(false); notify('Note saved.');
      void load('save');
      onChanged?.();
    } catch (err) {
      if (current !== version.current) return;
      setBusy(false); setNeedsReload(true); setConfirming(false);
      setError(err instanceof ApiError && err.status === 409
        ? 'This note changed or the group is full. Copy your draft, then reload notes before trying again.'
        : 'Could not confirm the change. Copy your draft, then reload notes before trying again.');
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
      await api.post(`/groups/${groupId}/notes/${encodeURIComponent(selected.name)}/delete`, { expected_revision: selected.revision });
      if (current !== version.current) return;
      setBusy(false); setConfirming(false); notify('Note deleted.');
      void load('delete');
      onChanged?.();
    } catch (err) {
      if (current !== version.current) return;
      setBusy(false); setNeedsReload(true); setConfirming(false);
      setError(err instanceof ApiError && err.status === 409
        ? 'This note changed or was removed. Reload notes before trying again.'
        : 'Could not confirm the deletion. Reload notes and review before trying again.');
    } finally {
      if (mutationInFlight.current === mutation) mutationInFlight.current = null;
    }
  }

  return <section className="page-card group-panel group-notes" aria-labelledby="group-notes-title">
    <div className="panel-title"><div><span className="eyebrow">KEEP THE GOOD STUFF CLOSE</span><h3 id="group-notes-title">Group notes</h3><p>Share a helpful answer, guide, or reminder on demand.</p></div><FileText size={20} /></div>
    <div className="notes-public-notice"><strong>Public in this group</strong><span>Anyone who can see group messages may read or share notes. Use /note &lt;name&gt;; don’t save private information or secrets.</span></div>
    {preview && <p className="field-hint">Sample data only · notes cannot be changed in this preview.</p>}
    {loading && <p className="muted-small" role="status">Loading notes…</p>}
    {error && <p className="notes-error" role="alert">{error}</p>}
    {needsReload && <button className="text-action notes-reload" type="button" disabled={busy || loading} onClick={() => void load()}>Reload notes (discard draft)</button>}
    {!loading && notes === null && <button className="button button-subtle" type="button" onClick={() => void load()}>Try again</button>}
    {notes && <div className="notes-layout">
      <aside className="notes-sidebar" aria-label="Saved notes">
        <div className="notes-list-heading"><span>SAVED NOTES <small>{notes.length} / 30</small></span><button className="notes-new" type="button" aria-label="New note" disabled={preview || busy || loading || confirming || needsReload || notes.length >= 30} onClick={() => select(null)}><Plus size={16} /> New</button></div>
        {notes.length ? <ul className="notes-list">{notes.map(note => <li key={note.name}><button type="button" aria-label={`${preview ? 'View' : 'Edit'} note ${note.name}`} aria-pressed={selectedName === note.name} disabled={busy || loading || confirming || needsReload} onClick={() => select(note)}><strong>{note.name}</strong><small>{note.content}</small></button></li>)}</ul> : <p className="muted-small">No notes yet. Create the first one.</p>}
        {notes.length >= 30 && <p className="muted-small">The 30-note limit has been reached.</p>}
      </aside>
      <form className="notes-editor" onSubmit={save}>
        <div className="notes-editor-heading"><span className="eyebrow">{selected ? 'EDITING NOTE' : 'NEW NOTE'}</span>{selected && <strong>{selected.name}</strong>}</div>
        {!selected && <label>Note name<input aria-label="Note name" aria-describedby="note-name-hint" value={name} maxLength={32} disabled={preview || busy || loading || confirming} readOnly={needsReload} onChange={event => setName(event.target.value.toLowerCase())} placeholder="e.g. getting-started" autoComplete="off" /><small id="note-name-hint">Lowercase letters, numbers, hyphens and underscores.</small></label>}
        <label>Note text<textarea aria-label="Note text" aria-describedby="note-text-hint" value={content} maxLength={2400} disabled={preview || busy || loading || confirming} readOnly={needsReload} onChange={event => setContent(event.target.value)} placeholder="Write something useful for your group…" /><small id="note-text-hint">{length} / 1200 characters · text only</small></label>
        <div className="notes-actions"><button className="button button-primary" type="submit" disabled={preview || busy || loading || confirming || needsReload || (selected === null && notes.length >= 30)}>{busy ? 'Saving…' : 'Save note'}</button>{dirty && !preview && <button className="button button-subtle" type="button" disabled={busy || loading || needsReload} onClick={discardDraft}>Discard draft</button>}{selected && <button className="notes-delete" type="button" ref={openerRef} disabled={preview || busy || loading || needsReload || dirty} onClick={() => setConfirming(true)}><Trash2 size={15} /> Delete note</button>}</div>
      </form>
    </div>}
    {confirming && selected && <div ref={confirmationRef} tabIndex={-1} className="warning-confirm" role="region" aria-label="Confirm note deletion"><h4>Delete this note?</h4><p>Remove “{selected.name}” for everyone in this group? This cannot be undone in SUMMON. The removal will be recorded without the note text.</p><div className="warning-confirm-actions"><button className="button button-subtle" type="button" disabled={busy} onClick={() => { setConfirming(false); openerRef.current?.focus(); }}>Cancel</button><button className="button button-primary" type="button" disabled={busy} onClick={() => void remove()}>{busy ? 'Deleting…' : 'Confirm deletion'}</button></div></div>}
  </section>;
}
