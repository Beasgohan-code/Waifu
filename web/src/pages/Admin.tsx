import { ArrowLeft, ArrowRight, Eye, Image, ShieldCheck } from 'lucide-react';
import { useEffect, useRef, useState } from 'react';
import { api, ApiError } from '../api';
import { rarities, rarityLabel } from '../rarities';
import type { Character, Rarity } from '../types';

type CatalogEntry = Pick<Character, 'id' | 'name' | 'category' | 'rarity' | 'image_url' | 'description' | 'active' | 'media_type'>;
type EditState = { revision: string; character: CatalogEntry | null };
type DraftGuard = { blocked: boolean };

function validEditState(value: EditState, id: string): boolean {
  if (!value || !/^[0-9a-f]{64}$/.test(value.revision)) return false;
  const entry = value.character;
  return entry === null || Boolean(entry && entry.id === id && typeof entry.name === 'string'
    && typeof entry.category === 'string' && typeof entry.rarity === 'string'
    && typeof entry.image_url === 'string' && typeof entry.description === 'string'
    && typeof entry.active === 'boolean' && (entry.media_type === undefined || ['photo', 'video', 'animation'].includes(entry.media_type)));
}

// The server remains authoritative; this prevents loading clearly invalid schemes
// or local application paths in a browser before the operator chooses to save.
function previewableArtwork(value: string): boolean {
  if (/^\/art\/[a-zA-Z0-9_-]+\.(?:png|jpe?g|webp)$/.test(value) || /^\/api\/artwork\/[0-9a-f]{64}$/.test(value)) return true;
  try {
    const url = new URL(value);
    const host = url.hostname.toLowerCase().replace(/\.$/, '');
    const ipv4 = host.match(/^(\d+)\.(\d+)\.(\d+)\.(\d+)$/)?.slice(1).map(Number);
    const localV4 = ipv4 && (ipv4[0] === 0 || ipv4[0] === 10 || ipv4[0] === 127 || ipv4[0] >= 224
      || (ipv4[0] === 169 && ipv4[1] === 254) || (ipv4[0] === 192 && ipv4[1] === 168)
      || (ipv4[0] === 172 && ipv4[1] >= 16 && ipv4[1] <= 31)
      || (ipv4[0] === 100 && ipv4[1] >= 64 && ipv4[1] <= 127));
    const localV6 = host.startsWith('[') && /^\[(?:::|::1\]|::ffff:|f[cd]|fe[89ab]|ff)/.test(host);
    return url.protocol === 'https:' && !url.username && !url.password && !url.hash && Boolean(host)
      && !localV4 && !localV6 && host !== 'localhost' && !host.endsWith('.localhost')
      && !host.endsWith('.local') && !host.endsWith('.internal');
  } catch { return false; }
}

/** URL-based catalogue editing only; media hosting remains in the Telegram flow. */
export default function Admin({ notify, refresh, onDraftChange, preview = false }: {
  notify: (message: string, error?: boolean) => void; refresh: () => void;
  onDraftChange?: (guard: DraftGuard | null) => void; preview?: boolean;
}) {
  const [id, setId] = useState(''); const [name, setName] = useState(''); const [category, setCategory] = useState('');
  const [rarity, setRarity] = useState<Rarity>('common'); const [image, setImage] = useState(''); const [description, setDescription] = useState('');
  const [active, setActive] = useState(true); const [busy, setBusy] = useState(false);
  const [review, setReview] = useState(false); const [acknowledged, setAcknowledged] = useState(false);
  const [editState, setEditState] = useState<EditState | null>(null); const [blockedSave, setBlockedSave] = useState(false);
  const [checking, setChecking] = useState<'load' | 'review' | null>(null); const [notice, setNotice] = useState('');
  const [loadArtwork, setLoadArtwork] = useState(false); const [artworkError, setArtworkError] = useState(false);
  const [error, setError] = useState(''); const pending = useRef(false);
  const reviewHeading = useRef<HTMLHeadingElement>(null);
  const nameInput = useRef<HTMLInputElement>(null);
  const snapshot = JSON.stringify([id, name, category, rarity, image, description, active]);
  const [baseline, setBaseline] = useState(() => JSON.stringify(['', '', '', 'common', '', '', true]));
  const dirty = snapshot !== baseline;
  const canLoad = /^[a-z0-9][a-z0-9-]{0,63}$/.test(id) && !name && !category && !image && !description && rarity === 'common' && active;
  const currentMedia = editState?.character;
  const replacingMotion = Boolean(currentMedia && ['video', 'animation'].includes(currentMedia.media_type || '') && image.trim() !== currentMedia.image_url);
  const reviewingVideo = currentMedia?.media_type === 'video' && image.trim() === currentMedia.image_url;
  useEffect(() => { onDraftChange?.(dirty || busy ? { blocked: busy } : null); }, [dirty, busy, onDraftChange]);
  useEffect(() => () => { onDraftChange?.(null); }, [onDraftChange]);
  useEffect(() => {
    if (!dirty && !busy) return;
    function warn(event: BeforeUnloadEvent) { event.preventDefault(); event.returnValue = ''; }
    window.addEventListener('beforeunload', warn);
    return () => window.removeEventListener('beforeunload', warn);
  }, [dirty, busy]);
  useEffect(() => { if (review) reviewHeading.current?.focus(); }, [review]);
  useEffect(() => { if (notice && !review) nameInput.current?.focus(); }, [notice, review]);

  async function loadExisting() {
    if (!canLoad || pending.current) return;
    pending.current = true; setBusy(true); setChecking('load'); setError(''); setNotice('');
    try {
      const state = await api.get<EditState>(`/admin/characters/${encodeURIComponent(id)}/edit-state`);
      if (!validEditState(state, id)) throw new Error('Invalid review state');
      const entry = state.character;
      if (!entry) { setError('No existing character uses this exact ID.'); return; }
      if (!rarities.some(option => option.value === entry.rarity)) {
        setError('This character has an unsupported rarity; ask an operator to review it.'); return;
      }
      setName(entry.name); setCategory(entry.category); setRarity(entry.rarity);
      setImage(entry.image_url); setDescription(entry.description); setActive(entry.active);
      setBaseline(JSON.stringify([id, entry.name, entry.category, entry.rarity, entry.image_url, entry.description, entry.active]));
      setNotice(entry.media_type === 'video' || entry.media_type === 'animation'
        ? 'Existing motion artwork loaded. Keep its URL unchanged to retain its Telegram file reference; use /upload for new video or GIF media.'
        : 'Existing details loaded; no changes have been published.');
    } catch {
      setError('Could not load the current catalogue entry. Your draft has not been saved.');
    } finally { pending.current = false; setBusy(false); setChecking(null); }
  }

  async function reviewDraft(event: React.FormEvent) {
    event.preventDefault();
    if (pending.current) return;
    if (!name.trim() || !category.trim() || !image.trim()) {
      setError('Name, series and artwork URL cannot be blank.');
      return;
    }
    if (!previewableArtwork(image.trim())) {
      setError('Use a public HTTPS artwork URL or a supported local artwork path.');
      return;
    }
    pending.current = true; setBusy(true); setChecking('review'); setError(''); setNotice('');
    try {
      const state = await api.get<EditState>(`/admin/characters/${encodeURIComponent(id)}/edit-state`);
      if (!validEditState(state, id)) throw new Error('Invalid review state');
      setEditState(state); setAcknowledged(false); setBlockedSave(false);
      setLoadArtwork(false); setArtworkError(false); setReview(true);
    } catch {
      setError('Could not check the current catalogue entry. Your draft has not been saved.');
    } finally { pending.current = false; setBusy(false); setChecking(null); }
  }

  async function save() {
    if (preview || !acknowledged || blockedSave || !editState || pending.current) return;
    pending.current = true; setBusy(true); setError('');
    try {
      await api.put(`/admin/characters/${encodeURIComponent(id)}/reviewed`, {
        character: { id, name: name.trim(), category: category.trim(), rarity, image_url: image.trim(), description: description.trim(), active },
        expected_revision: editState.revision,
      });
      setBaseline(snapshot); notify('Character saved to the catalogue.'); refresh(); setReview(false);
    } catch (err) {
      // A timeout or server failure can occur after commit. Never automatically retry.
      const definite = err instanceof ApiError && [400, 401, 403, 404, 409, 422, 429].includes(err.status);
      const stale = err instanceof ApiError && err.status === 409;
      const message = stale ? `${err.message}. Return to details and review again.`
        : definite ? err.message : 'Save could not be confirmed. Check the catalogue before trying again.';
      if (stale || !definite) setBlockedSave(true);
      setError(message); notify(message, true);
    } finally { pending.current = false; setBusy(false); }
  }

  return <div className="page-content"><div className="page-heading"><span className="eyebrow">CATALOGUE OPERATIONS</span><h1>Curate the universe.</h1><p>{preview ? 'Read-only sample editor; saving is disabled.' : 'Only server-authorized catalogue operators can make changes here.'}</p></div>
    <div className="studio-steps" aria-label="Catalogue editor steps"><span className={!review ? 'current' : ''}>01 · Details</span><span aria-hidden="true">—</span><span className={review ? 'current' : ''}>02 · Review &amp; save</span></div>
    {!review ? <form className="page-card group-panel" onSubmit={reviewDraft}><div className="panel-title"><div><span className="eyebrow">CHARACTER METADATA</span><h3>Add or update a character</h3><p>Enter an existing public photo URL. For videos and GIFs, use the bot's /upload flow.</p></div><ShieldCheck size={20} /></div>
      <fieldset className="studio-fieldset" disabled={busy}><div className="form-grid group-form"><div className="studio-id-field"><label>Character ID<input value={id} onChange={event => { setId(event.target.value); setNotice(''); }} pattern="[a-z0-9][a-z0-9-]*" maxLength={64} required placeholder="e.g. aiko-tsukino" /></label><button className="text-action" type="button" disabled={!canLoad} onClick={loadExisting}>{checking === 'load' ? 'Loading…' : 'Load existing details'}</button></div><label>Name<input ref={nameInput} value={name} onChange={event => setName(event.target.value)} maxLength={120} required /></label><label>Series / category<input value={category} onChange={event => setCategory(event.target.value)} maxLength={120} required /></label><label>Rarity<select value={rarity} onChange={event => setRarity(event.target.value as Rarity)}>{rarities.map(item => <option key={item.value} value={item.value}>{item.label}</option>)}</select></label><label className="wide-field">Artwork URL<input value={image} onChange={event => setImage(event.target.value)} maxLength={2048} required placeholder="https://… or /art/filename.webp" /></label><label className="wide-field">Description<textarea value={description} onChange={event => setDescription(event.target.value)} maxLength={1200} /></label></div>
      <p className="muted-small">To load an existing record, enter its exact ID before filling the other fields. Loading does not save changes.</p>
      <label className="check-line"><input type="checkbox" checked={active} onChange={event => setActive(event.target.checked)} /> Available in the catalogue and for spawning</label>
      {notice && <p className="muted-small" role="status">{notice}</p>}
      {error && <p className="studio-error" role="alert">{error}</p>}
      <button className="button button-primary" type="submit">{checking === 'review' ? 'Checking catalogue…' : 'Review character'} <ArrowRight size={15} /></button></fieldset>
    </form> : <section className="page-card group-panel studio-review" aria-labelledby="studio-review-heading">
      <div className="panel-title"><div><span className="eyebrow">REVIEW BEFORE SAVING</span><h3 id="studio-review-heading" ref={reviewHeading} tabIndex={-1}>Check the details.</h3><p>Nothing has been published yet. Saving will update this ID without changing ownership; changes to editable details since review will be rejected.</p></div><Eye size={20} /></div>
      {editState?.character ? <div className="studio-current"><h4>Current record for this ID</h4><dl><div><dt>Name</dt><dd>{editState.character.name}</dd></div><div><dt>Series</dt><dd>{editState.character.category}</dd></div><div><dt>Rarity</dt><dd>{rarityLabel(editState.character.rarity)}</dd></div><div><dt>Availability</dt><dd>{editState.character.active ? 'Active' : 'Inactive'}</dd></div><div><dt>Media</dt><dd>{editState.character.media_type === 'video' ? 'Video' : editState.character.media_type === 'animation' ? 'Animation' : 'Photo'}</dd></div><div><dt>Artwork URL</dt><dd className="studio-url">{editState.character.image_url}</dd></div><div><dt>Description</dt><dd>{editState.character.description || '—'}</dd></div></dl></div> : <p className="studio-current">No record used this ID when checked.</p>}
      <div className="studio-preview"><div className="studio-preview-art">
        {loadArtwork && !artworkError ? reviewingVideo ? <video src={image.trim()} aria-label={`Artwork preview for ${name.trim()}`} controls playsInline preload="metadata" onError={() => setArtworkError(true)} /> : <img src={image.trim()} alt={`Artwork preview for ${name.trim()}`} onError={() => setArtworkError(true)} /> : <div><Image size={26} aria-hidden="true" /><span>{artworkError ? 'Artwork could not be loaded.' : 'Artwork is not loaded yet.'}</span><button className="text-action" type="button" onClick={() => { setArtworkError(false); setLoadArtwork(true); }}>{artworkError ? 'Try loading artwork' : 'Load artwork preview'}</button></div>}
      </div><div className="studio-preview-copy"><span className="eyebrow">{rarityLabel(rarity)}</span><h2>{name.trim()}</h2><p>{category.trim()}</p><dl><div><dt>Catalog ID</dt><dd>{id}</dd></div><div><dt>Availability</dt><dd>{active ? 'Active' : 'Inactive'}</dd></div><div><dt>Artwork URL</dt><dd className="studio-url">{image.trim()}</dd></div></dl>{description.trim() && <p className="studio-description">{description.trim()}</p>}</div></div>
      <p className="muted-small">Loading a preview contacts the artwork host from your browser. Use a trusted public URL and publish only artwork you have permission to share. The server validates the URL when saving.</p>
      {replacingMotion && <p className="studio-error" role="status">Changing this URL replaces the existing motion artwork and removes its Telegram file reference. Use /upload for new video or GIF media.</p>}
      <label className="check-line"><input type="checkbox" checked={acknowledged} onChange={event => setAcknowledged(event.target.checked)} disabled={busy} /> I understand an existing ID will be updated and the artwork is public.</label>
      {error && <p className="studio-error" role="alert">{error}</p>}
      <div className="studio-actions"><button className="button button-subtle" type="button" disabled={busy} onClick={() => { setReview(false); setError(''); }}><ArrowLeft size={15} /> Edit details</button><button className="button button-primary" type="button" disabled={preview || busy || blockedSave || !acknowledged} onClick={save}>{preview ? 'Preview only' : busy ? 'Saving…' : 'Confirm & save'} <ArrowRight size={15} /></button></div>
    </section>}
  </div>;
}
