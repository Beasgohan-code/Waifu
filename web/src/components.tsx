import { ArrowRight, Bookmark, Check, ChevronRight, Heart, Sparkles, X } from 'lucide-react';
import { useEffect, useRef, useState } from 'react';
import { api } from './api';
import type { Character } from './types';

export function Brand({ compact = false }: { compact?: boolean }) {
  return <div className={`brand ${compact ? 'brand-compact' : ''}`} aria-label="SUMMON">
    <span className="brand-mark"><Sparkles size={21} strokeWidth={1.7} /></span>
    <span className="brand-word">SUMMON<span className="brand-dot">.</span><small>COLLECT · DISCOVER · SUMMON</small></span>
  </div>;
}

export function SectionTitle({ eyebrow, title, action, onAction }: { eyebrow?: string; title: string; action?: string; onAction?: () => void }) {
  return <div className="section-title">
    <div>{eyebrow && <span className="eyebrow">{eyebrow}</span>}<h2>{title}</h2></div>
    {action && onAction && <button className="text-action" onClick={onAction}>{action}<ArrowRight size={15} /></button>}
  </div>;
}

export function EmptyState({ title, text, action, onAction }: { title: string; text: string; action?: string; onAction?: () => void }) {
  return <div className="empty-state"><span className="empty-icon"><Sparkles size={23} /></span><h3>{title}</h3><p>{text}</p>
    {action && onAction && <button className="button button-subtle" onClick={onAction}>{action}<ArrowRight size={15} /></button>}
  </div>;
}

export function Skeleton({ count = 3 }: { count?: number }) {
  return <div className="character-grid" aria-label="Loading characters" aria-busy="true">{Array.from({ length: count }, (_, i) => <div className="skeleton-card" key={i}><span /><i /><i /></div>)}</div>;
}

export function ProgressBar({ value, max, label }: { value: number; max: number; label?: string }) {
  const percent = max > 0 ? Math.min(100, Math.max(0, value / max * 100)) : 0;
  return <div className="progress-track" role="progressbar" aria-label={label || 'Progress'} aria-valuenow={value} aria-valuemin={0} aria-valuemax={max || 1}><span style={{ width: `${percent}%` }} /></div>;
}

export function CharacterCard({ character, onOpen, onFavorite, preview }: {
  character: Character; onOpen: (id: string) => void; onFavorite?: (character: Character) => void; preview: boolean;
}) {
  return <article className="character-card">
    <div className="character-art">
      <button className="art-button" onClick={() => onOpen(character.id)} aria-label={`View ${character.name}`}>
        <img src={character.image_url} alt={character.name} loading="lazy" />
        <span className="art-shade" />
      </button>
      <span className={`rarity rarity-${character.rarity}`}><span>✦</span> {character.rarity}</span>
      {character.quantity > 0 && <span className="owned-badge"><Check size={12} /> OWNED{character.quantity > 1 ? ` · ×${character.quantity}` : ''}</span>}
      {character.quantity > 0 && onFavorite && <button className={`heart-button ${character.favorite ? 'is-favorite' : ''}`}
        onClick={() => onFavorite(character)} disabled={preview} title={preview ? 'Read-only preview' : character.favorite ? 'Remove favorite' : 'Add favorite'}
        aria-label={`${character.favorite ? 'Remove' : 'Add'} ${character.name} ${character.favorite ? 'from' : 'to'} favorites`}><Heart size={16} fill={character.favorite ? 'currentColor' : 'none'} /></button>}
    </div>
    <button className="character-info" onClick={() => onOpen(character.id)}>
      <span><strong>{character.name}</strong><small>{character.category}</small></span><ChevronRight size={17} />
    </button>
  </article>;
}

export function CharacterDetail({ id, preview, onClose, onChanged, notify }: {
  id: string; preview: boolean; onClose: () => void; onChanged: () => void; notify: (message: string, error?: boolean) => void;
}) {
  const [character, setCharacter] = useState<Character | null>(null);
  const [error, setError] = useState('');
  const [saving, setSaving] = useState(false);
  const closeRef = useRef<HTMLButtonElement>(null);
  const modalRef = useRef<HTMLElement>(null);
  useEffect(() => {
    let active = true;
    const opener = document.activeElement instanceof HTMLElement ? document.activeElement : null;
    const previousOverflow = document.body.style.overflow;
    document.body.style.overflow = 'hidden';
    api.get<Character>(`/characters/${encodeURIComponent(id)}`).then(value => { if (active) setCharacter(value); })
      .catch((err: Error) => { if (active) setError(err.message); });
    closeRef.current?.focus();
    const handleKey = (event: KeyboardEvent) => {
      if (event.key === 'Escape') { onClose(); return; }
      if (event.key !== 'Tab') return;
      const items = Array.from(modalRef.current?.querySelectorAll<HTMLElement>(
        'button:not(:disabled), a[href], input:not(:disabled), select:not(:disabled), textarea:not(:disabled), [tabindex]:not([tabindex="-1"])',
      ) || []);
      if (!items.length) { event.preventDefault(); return; }
      const first = items[0]; const last = items[items.length - 1];
      if (event.shiftKey && (document.activeElement === first || !modalRef.current?.contains(document.activeElement))) {
        event.preventDefault(); last.focus();
      } else if (!event.shiftKey && (document.activeElement === last || !modalRef.current?.contains(document.activeElement))) {
        event.preventDefault(); first.focus();
      }
    };
    window.addEventListener('keydown', handleKey);
    return () => {
      active = false;
      window.removeEventListener('keydown', handleKey);
      document.body.style.overflow = previousOverflow;
      if (opener?.isConnected) opener.focus();
    };
  }, [id, onClose]);
  async function toggle(field: 'favorite' | 'wishlist') {
    if (!character || preview || saving) return;
    setSaving(true);
    try {
      const current = field === 'favorite' ? character.favorite : character.wishlisted;
      await api.put(`/characters/${encodeURIComponent(id)}/${field}`, { value: !current });
      setCharacter({ ...character, [field === 'favorite' ? 'favorite' : 'wishlisted']: !current });
      notify(field === 'favorite' ? (!current ? 'Added to favorites' : 'Removed from favorites') : (!current ? 'Added to your wishlist' : 'Removed from your wishlist'));
      onChanged();
    } catch (err) { notify(err instanceof Error ? err.message : 'Could not save this change', true); }
    finally { setSaving(false); }
  }
  return <div className="modal-backdrop" onMouseDown={event => { if (event.target === event.currentTarget) onClose(); }}>
    <section ref={modalRef} className="detail-modal" role="dialog" aria-modal="true" aria-label="Character details">
      <button ref={closeRef} className="modal-close icon-button" onClick={onClose} aria-label="Close character details"><X size={19} /></button>
      {error ? <EmptyState title="Couldn't load character" text={error} /> : !character ? <div className="detail-loading" aria-busy="true">Loading character…</div> : <>
        <div className="detail-art"><img src={character.image_url} alt={character.name} /><span className={`rarity rarity-${character.rarity}`}>✦ {character.rarity}</span></div>
        <div className="detail-copy"><span className="eyebrow">THE SUMMON ARCHIVE / {character.id}</span><h2>{character.name}</h2><p className="detail-series">{character.category}</p>
          <p className="detail-description">{character.description || 'A character waiting to be discovered.'}</p>
          <div className="detail-facts"><div><span>RARITY</span><strong>{character.rarity}</strong></div><div><span>IN YOUR COLLECTION</span><strong>{character.quantity ? `${character.quantity} owned` : 'Not yet collected'}</strong></div></div>
          <div className="detail-actions">
            {character.quantity > 0 && <button className="button button-primary" disabled={preview || saving} onClick={() => toggle('favorite')}><Heart size={16} fill={character.favorite ? 'currentColor' : 'none'} />{character.favorite ? 'Favorited' : 'Add favorite'}</button>}
            <button className="button button-subtle" disabled={preview || saving} onClick={() => toggle('wishlist')}><Bookmark size={16} fill={character.wishlisted ? 'currentColor' : 'none'} />{character.wishlisted ? 'Wishlisted' : 'Add to wishlist'}</button>
          </div>
          {preview && <p className="preview-note">Sample character · actions are disabled in preview.</p>}
        </div>
      </>}
    </section>
  </div>;
}
