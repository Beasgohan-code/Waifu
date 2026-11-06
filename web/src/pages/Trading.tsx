import { ArrowLeftRight, ArrowRight, Clock3, Plus, X } from 'lucide-react';
import { useState } from 'react';
import { api } from '../api';
import { EmptyState, SectionTitle } from '../components';
import { useRemote } from '../hooks';
import type { Character, Page, Profile, Trade } from '../types';
import Gifts from './Gifts';

interface Props { profile: Profile; preview: boolean; revision: number; refresh: () => void; notify: (message: string, error?: boolean) => void }
export default function Trading(props: Props) {
  const [mode, setMode] = useState<'trade' | 'gift'>('trade');
  return <div className="page-content"><div className="page-heading"><span className="eyebrow">A LITTLE GIVE AND TAKE</span><h1>{mode === 'trade' ? 'Trade with friends.' : 'A gift from the heart.'}</h1><p>Share your collection thoughtfully; transfers happen only after the recipient accepts.</p></div>
    <div className="segment exchange-segment" aria-label="Exchange type"><button className={mode === 'trade' ? 'active' : ''} aria-pressed={mode === 'trade'} onClick={() => setMode('trade')}>Trades</button><button className={mode === 'gift' ? 'active' : ''} aria-pressed={mode === 'gift'} onClick={() => setMode('gift')}>Gifts</button></div>
    {mode === 'trade' ? <TradePanel {...props} /> : <Gifts {...props} />}
  </div>;
}

function TradePanel({ profile, preview, revision, refresh, notify }: Props) {
  const { data: trades, loading, error } = useRemote<Trade[]>('/trades', revision);
  const { data: inventory } = useRemote<Page<Character>>('/inventory?limit=50', revision);
  const [creating, setCreating] = useState(false);
  const [receiver, setReceiver] = useState('');
  const [offer, setOffer] = useState('');
  const [request, setRequest] = useState('');
  const [busy, setBusy] = useState(false);
  async function propose(event: React.FormEvent) {
    event.preventDefault();
    if (preview || busy) return;
    const id = Number(receiver);
    if (!Number.isSafeInteger(id) || id <= 0 || !offer || !request.trim()) { notify('Enter a valid player ID and both character IDs.', true); return; }
    setBusy(true);
    try { await api.post('/trades', { receiver_id: id, offer_id: offer, request_id: request.trim().toLowerCase() });
      notify('Trade offer created. It expires in 15 minutes.'); setCreating(false); setReceiver(''); setRequest(''); refresh();
    } catch (err) { notify(err instanceof Error ? err.message : 'Could not create the trade.', true); } finally { setBusy(false); }
  }
  async function act(trade: Trade, action: 'accept' | 'cancel') {
    if (preview || busy) return;
    setBusy(true);
    try { await api.post(`/trades/${trade.id}/${action}`); notify(action === 'accept' ? 'Trade completed.' : 'Trade cancelled.'); refresh(); }
    catch (err) { notify(err instanceof Error ? err.message : 'Could not update the trade.', true); } finally { setBusy(false); }
  }
  return <><div className="page-card trade-intro"><span className="large-icon"><ArrowLeftRight size={24} /></span><div><h2>Make someone's day.</h2><p>Offer one of your characters for a friend's character. Offers expire after 15 minutes and can be cancelled by either player.</p></div>
      <button className="button button-primary" disabled={preview || !inventory?.items.length} onClick={() => setCreating(true)}><Plus size={16} /> New trade</button></div>
    {preview && <p className="inline-note">This is a read-only preview. Trading is available after signing in through Telegram.</p>}
    {creating && <form className="page-card form-card" onSubmit={propose}><div className="form-title"><div><span className="eyebrow">A NEW EXCHANGE</span><h2>Create a trade offer</h2></div><button type="button" className="icon-button" onClick={() => setCreating(false)} aria-label="Close trade form"><X size={18} /></button></div>
      <div className="form-grid"><label>Recipient's Telegram user ID<input value={receiver} onChange={event => setReceiver(event.target.value)} inputMode="numeric" required placeholder="e.g. 123456789" /></label>
        <label>Your character<select value={offer} onChange={event => setOffer(event.target.value)} required><option value="">Choose an owned character</option>{inventory?.items.map(char => <option key={char.id} value={char.id}>{char.name} · {char.id}</option>)}</select></label>
        <label>Character ID to request<input value={request} onChange={event => setRequest(event.target.value)} required maxLength={64} pattern="[a-z0-9][a-z0-9-]*" placeholder="e.g. aiko" /></label></div>
      <p className="field-hint">You can also reply to a friend in a group with <code>/trade your-id their-id</code>.</p>
      <button className="button button-primary" disabled={busy} type="submit">Send offer <ArrowRight size={15} /></button></form>}
    <div className="spaced-section"><SectionTitle eyebrow="YOUR EXCHANGES" title="Trade history" />
      {loading ? <div className="loading-line">Loading trades…</div> : error ? <EmptyState title="Couldn't load trades" text={error} /> : !trades?.length ? <EmptyState title="No trades yet" text="Your offers and completed exchanges will appear here." /> :
        <div className="list-card">{trades.map(trade => {
          const expired = trade.status === 'pending' && new Date(trade.expires_at).getTime() <= Date.now();
          const status = expired ? 'expired' : trade.status;
          return <div className="trade-row" key={trade.id}><span className="list-icon"><ArrowLeftRight size={18} /></span><div className="list-main"><strong>{trade.offer_id} <ArrowRight size={13} /> {trade.request_id}</strong><small>{trade.sender_id === profile.id ? `Offered to ${trade.receiver_id}` : `Offered by ${trade.sender_id}`} · <Clock3 size={12} /> {new Date(trade.created_at).toLocaleDateString()}</small></div><span className={`status-pill status-${status}`}>{status}</span>
            {status === 'pending' && !preview && <div className="row-actions">{trade.receiver_id === profile.id && <button className="button button-small" disabled={busy} onClick={() => act(trade, 'accept')}>Accept</button>}<button className="text-action" disabled={busy} onClick={() => act(trade, 'cancel')}>Cancel</button></div>}</div>;
        })}</div>}
    </div>
  </>;
}
