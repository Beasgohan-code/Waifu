import { rarities } from '../rarities';
import { ArrowRight, ShieldCheck } from 'lucide-react';
import { useState } from 'react';
import { api } from '../api';
import type { Rarity } from '../types';

export default function Admin({ notify, refresh }: { notify: (message: string, error?: boolean) => void; refresh: () => void }) {
  const [id, setId] = useState(''); const [name, setName] = useState(''); const [category, setCategory] = useState('');
  const [rarity, setRarity] = useState<Rarity>('common'); const [image, setImage] = useState(''); const [description, setDescription] = useState('');
  const [active, setActive] = useState(true); const [busy, setBusy] = useState(false);
  async function submit(event: React.FormEvent) {
    event.preventDefault(); setBusy(true);
    try { await api.put(`/admin/characters/${encodeURIComponent(id)}`, { id, name, category, rarity, image_url: image, description, active });
      notify('Character saved to the catalogue.'); refresh();
    } catch (err) { notify(err instanceof Error ? err.message : 'Could not save the character.', true); } finally { setBusy(false); }
  }
  return <div className="page-content"><div className="page-heading"><span className="eyebrow">CATALOGUE OPERATIONS</span><h1>Curate the universe.</h1><p>Only server-authorized catalogue operators can make changes here.</p></div>
    <form className="page-card group-panel" onSubmit={submit}><div className="panel-title"><div><span className="eyebrow">CHARACTER METADATA</span><h3>Add or update a character</h3><p>Saving an existing ID updates its metadata without changing ownership.</p></div><ShieldCheck size={20} /></div>
      <div className="form-grid group-form"><label>Character ID<input value={id} onChange={event => setId(event.target.value)} pattern="[a-z0-9][a-z0-9-]*" maxLength={64} required placeholder="e.g. aiko-tsukino" /></label><label>Name<input value={name} onChange={event => setName(event.target.value)} maxLength={120} required /></label><label>Series / category<input value={category} onChange={event => setCategory(event.target.value)} maxLength={120} required /></label><label>Rarity<select value={rarity} onChange={event => setRarity(event.target.value as Rarity)}>{rarities.map(item => <option key={item.value} value={item.value}>{item.label}</option>)}</select></label><label className="wide-field">Artwork URL<input value={image} onChange={event => setImage(event.target.value)} maxLength={2048} required placeholder="https://… or /art/filename.webp" /></label><label className="wide-field">Description<textarea value={description} onChange={event => setDescription(event.target.value)} maxLength={1200} /></label></div>
      <label className="check-line"><input type="checkbox" checked={active} onChange={event => setActive(event.target.checked)} /> Available in the catalogue and for spawning</label><button className="button button-primary" disabled={busy} type="submit">Save character <ArrowRight size={15} /></button>
    </form>
  </div>;
}
