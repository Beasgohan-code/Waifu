import { ArrowLeft, ArrowRight, Eye, Image, ShieldCheck } from 'lucide-react';
import { useEffect, useRef, useState } from 'react';
import { api, ApiError } from '../api';
import { rarities, rarityLabel } from '../rarities';
import type { Rarity } from '../types';

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
  onDraftChange?: (dirty: boolean) => void; preview?: boolean;
}) {
  const [id, setId] = useState(''); const [name, setName] = useState(''); const [category, setCategory] = useState('');
  const [rarity, setRarity] = useState<Rarity>('common'); const [image, setImage] = useState(''); const [description, setDescription] = useState('');
  const [active, setActive] = useState(true); const [busy, setBusy] = useState(false);
  const [review, setReview] = useState(false); const [acknowledged, setAcknowledged] = useState(false);
  const [loadArtwork, setLoadArtwork] = useState(false); const [artworkError, setArtworkError] = useState(false);
  const [error, setError] = useState(''); const pending = useRef(false);
  const reviewHeading = useRef<HTMLHeadingElement>(null);
  const snapshot = JSON.stringify([id, name, category, rarity, image, description, active]);
  const [baseline, setBaseline] = useState(() => JSON.stringify(['', '', '', 'common', '', '', true]));
  const dirty = snapshot !== baseline;
  useEffect(() => { onDraftChange?.(dirty || busy); }, [dirty, busy, onDraftChange]);
  useEffect(() => () => { onDraftChange?.(false); }, [onDraftChange]);
  useEffect(() => {
    if (!dirty && !busy) return;
    function warn(event: BeforeUnloadEvent) { event.preventDefault(); event.returnValue = ''; }
    window.addEventListener('beforeunload', warn);
    return () => window.removeEventListener('beforeunload', warn);
  }, [dirty, busy]);
  useEffect(() => { if (review) reviewHeading.current?.focus(); }, [review]);

  function reviewDraft(event: React.FormEvent) {
    event.preventDefault();
    if (!name.trim() || !category.trim() || !image.trim()) {
      setError('Name, series and artwork URL cannot be blank.');
      return;
    }
    if (!previewableArtwork(image.trim())) {
      setError('Use a public HTTPS artwork URL or a supported local artwork path.');
      return;
    }
    setError(''); setAcknowledged(false); setLoadArtwork(false); setArtworkError(false); setReview(true);
  }

  async function save() {
    if (preview || !acknowledged || pending.current) return;
    pending.current = true; setBusy(true); setError('');
    try {
      await api.put(`/admin/characters/${encodeURIComponent(id)}`, {
        id, name: name.trim(), category: category.trim(), rarity, image_url: image.trim(),
        description: description.trim(), active,
      });
      setBaseline(snapshot); notify('Character saved to the catalogue.'); refresh(); setReview(false);
    } catch (err) {
      // A network/server error can occur after a successful write. Do not auto-retry.
      const message = err instanceof ApiError && [400, 401, 403, 404, 409, 422, 429].includes(err.status)
        ? err.message : 'Save could not be confirmed. Check the catalogue before trying again.';
      setError(message); notify(message, true);
    } finally { pending.current = false; setBusy(false); }
  }

  return <div className="page-content"><div className="page-heading"><span className="eyebrow">CATALOGUE OPERATIONS</span><h1>Curate the universe.</h1><p>{preview ? 'Read-only sample editor; saving is disabled.' : 'Only server-authorized catalogue operators can make changes here.'}</p></div>
    <div className="studio-steps" aria-label="Catalogue editor steps"><span className={!review ? 'current' : ''}>01 · Details</span><span aria-hidden="true">—</span><span className={review ? 'current' : ''}>02 · Review &amp; save</span></div>
    {!review ? <form className="page-card group-panel" onSubmit={reviewDraft}><div className="panel-title"><div><span className="eyebrow">CHARACTER METADATA</span><h3>Add or update a character</h3><p>Enter an existing public photo URL. For videos and GIFs, use the bot's /upload flow.</p></div><ShieldCheck size={20} /></div>
      <div className="form-grid group-form"><label>Character ID<input value={id} onChange={event => setId(event.target.value)} pattern="[a-z0-9][a-z0-9-]*" maxLength={64} required placeholder="e.g. aiko-tsukino" /></label><label>Name<input value={name} onChange={event => setName(event.target.value)} maxLength={120} required /></label><label>Series / category<input value={category} onChange={event => setCategory(event.target.value)} maxLength={120} required /></label><label>Rarity<select value={rarity} onChange={event => setRarity(event.target.value as Rarity)}>{rarities.map(item => <option key={item.value} value={item.value}>{item.label}</option>)}</select></label><label className="wide-field">Artwork URL<input value={image} onChange={event => setImage(event.target.value)} maxLength={2048} required placeholder="https://… or /art/filename.webp" /></label><label className="wide-field">Description<textarea value={description} onChange={event => setDescription(event.target.value)} maxLength={1200} /></label></div>
      <label className="check-line"><input type="checkbox" checked={active} onChange={event => setActive(event.target.checked)} /> Available in the catalogue and for spawning</label>
      {error && <p className="studio-error" role="alert">{error}</p>}
      <button className="button button-primary" type="submit">Review character <ArrowRight size={15} /></button>
    </form> : <section className="page-card group-panel studio-review" aria-labelledby="studio-review-heading">
      <div className="panel-title"><div><span className="eyebrow">REVIEW BEFORE SAVING</span><h3 id="studio-review-heading" ref={reviewHeading} tabIndex={-1}>Check the details.</h3><p>Nothing has been published yet. An existing ID will be updated without changing ownership; concurrent edits may be overwritten.</p></div><Eye size={20} /></div>
      <div className="studio-preview"><div className="studio-preview-art">
        {loadArtwork && !artworkError ? <img src={image.trim()} alt={`Artwork preview for ${name.trim()}`} onError={() => setArtworkError(true)} /> : <div><Image size={26} aria-hidden="true" /><span>{artworkError ? 'Artwork could not be loaded.' : 'Artwork is not loaded yet.'}</span><button className="text-action" type="button" onClick={() => { setArtworkError(false); setLoadArtwork(true); }}>{artworkError ? 'Try loading artwork' : 'Load artwork preview'}</button></div>}
      </div><div className="studio-preview-copy"><span className="eyebrow">{rarityLabel(rarity)}</span><h2>{name.trim()}</h2><p>{category.trim()}</p><dl><div><dt>Catalog ID</dt><dd>{id}</dd></div><div><dt>Availability</dt><dd>{active ? 'Active' : 'Inactive'}</dd></div><div><dt>Artwork URL</dt><dd className="studio-url">{image.trim()}</dd></div></dl>{description.trim() && <p className="studio-description">{description.trim()}</p>}</div></div>
      <p className="muted-small">Loading a preview contacts the artwork host from your browser. Use a trusted public URL and publish only artwork you have permission to share. The server validates the URL when saving.</p>
      <label className="check-line"><input type="checkbox" checked={acknowledged} onChange={event => setAcknowledged(event.target.checked)} disabled={busy} /> I understand an existing ID will be updated, concurrent edits may be lost, and the artwork is public.</label>
      {error && <p className="studio-error" role="alert">{error}</p>}
      <div className="studio-actions"><button className="button button-subtle" type="button" disabled={busy} onClick={() => { setReview(false); setError(''); }}><ArrowLeft size={15} /> Edit details</button><button className="button button-primary" type="button" disabled={preview || busy || !acknowledged} onClick={save}>{preview ? 'Preview only' : busy ? 'Saving…' : 'Confirm & save'} <ArrowRight size={15} /></button></div>
    </section>}
  </div>;
}
