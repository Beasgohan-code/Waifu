import { ArrowRight, Clock3, Gift, Plus, X } from 'lucide-react';
import { useState } from 'react';
import { api } from '../api';
import { EmptyState, SectionTitle } from '../components';
import { useRemote } from '../hooks';
import type { Character, Gift as GiftRecord, Page, Profile } from '../types';

interface Props { profile: Profile; preview: boolean; revision: number; refresh: () => void; notify: (message: string, error?: boolean) => void }

export default function Gifts({ profile, preview, revision, refresh, notify }: Props) {
  const { data: gifts, loading, error } = useRemote<GiftRecord[]>('/gifts', revision);
  const { data: inventory } = useRemote<Page<Character>>('/inventory?limit=50', revision);
  const [creating, setCreating] = useState(false);
  const [receiver, setReceiver] = useState('');
  const [character, setCharacter] = useState('');
  const [busy, setBusy] = useState(false);

  async function propose(event: React.FormEvent) {
    event.preventDefault();
    if (preview || busy) return;
    const id = Number(receiver);
    if (!Number.isSafeInteger(id) || id <= 0 || !character) { notify('Choose a player and a character to gift.', true); return; }
    setBusy(true);
    try {
      await api.post('/gifts', { receiver_id: id, character_id: character });
      notify('Gift offered. The recipient has 15 minutes to accept.');
      setCreating(false); setReceiver(''); setCharacter(''); refresh();
    } catch (err) { notify(err instanceof Error ? err.message : 'Could not offer the gift.', true); }
    finally { setBusy(false); }
  }
  async function act(gift: GiftRecord, action: 'accept' | 'cancel') {
    if (preview || busy) return;
    setBusy(true);
    try { await api.post(`/gifts/${gift.id}/${action}`); notify(action === 'accept' ? 'Gift accepted.' : 'Gift cancelled.'); refresh(); }
    catch (err) { notify(err instanceof Error ? err.message : 'Could not update the gift.', true); }
    finally { setBusy(false); }
  }

  return <><div className="page-card trade-intro"><span className="large-icon"><Gift size={24} /></span><div><h2>A little kindness goes a long way.</h2><p>Offer a character to a friend. It stays in your collection until they accept, and the offer expires after 15 minutes.</p></div>
    <button className="button button-primary" disabled={preview || !inventory?.items.length} onClick={() => setCreating(true)}><Plus size={16} /> New gift</button></div>
    {preview && <p className="inline-note">This is a read-only preview. Gifting is available after signing in through Telegram.</p>}
    {creating && <form className="page-card form-card" onSubmit={propose}><div className="form-title"><div><span className="eyebrow">A LITTLE SOMETHING FOR SOMEONE</span><h2>Offer a gift</h2></div><button type="button" className="icon-button" onClick={() => setCreating(false)} aria-label="Close gift form"><X size={18} /></button></div>
      <div className="form-grid"><label>Recipient's Telegram user ID<input value={receiver} onChange={event => setReceiver(event.target.value)} inputMode="numeric" required placeholder="e.g. 123456789" /></label>
        <label>Your character<select value={character} onChange={event => setCharacter(event.target.value)} required><option value="">Choose an owned character</option>{inventory?.items.map(char => <option key={char.id} value={char.id}>{char.name} · {char.id}</option>)}</select></label></div>
      <p className="field-hint">You can also reply to a friend in a group with <code>/gift character-id</code>.</p>
      <button className="button button-primary" disabled={busy} type="submit">Offer gift <ArrowRight size={15} /></button></form>}
    <div className="spaced-section"><SectionTitle eyebrow="SHARED WITH CARE" title="Gift history" />
      {loading ? <div className="loading-line">Loading gifts…</div> : error ? <EmptyState title="Couldn't load gifts" text={error} /> : !gifts?.length ? <EmptyState title="No gifts yet" text="Your offered and received gifts will appear here." /> :
        <div className="list-card">{gifts.map(gift => {
          const expired = gift.status === 'pending' && new Date(gift.expires_at).getTime() <= Date.now();
          const status = expired ? 'expired' : gift.status;
          return <div className="trade-row" key={gift.id}><span className="list-icon"><Gift size={18} /></span><div className="list-main"><strong>{gift.character_id}</strong><small>{gift.sender_id === profile.id ? `Offered to ${gift.receiver_id}` : `Offered by ${gift.sender_id}`} · <Clock3 size={12} /> {new Date(gift.created_at).toLocaleDateString()}</small></div><span className={`status-pill status-${status}`}>{status}</span>
            {status === 'pending' && !preview && <div className="row-actions">{gift.receiver_id === profile.id && <button className="button button-small" disabled={busy} onClick={() => act(gift, 'accept')}>Accept gift</button>}<button className="text-action" disabled={busy} onClick={() => act(gift, 'cancel')}>Cancel</button></div>}</div>;
        })}</div>}
    </div>
  </>;
}
