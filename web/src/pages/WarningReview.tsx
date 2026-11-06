import { useEffect, useRef, useState } from 'react';
import { api } from '../api';
import type { WarningPage, WarningRecord } from '../types';

interface Props { groupId: number; preview: boolean; notify: (message: string, error?: boolean) => void }
interface Review { targetId: number; page: WarningPage; first: boolean }

/** Active warnings are scoped to a group/member. Confirmation never trusts stale UI state. */
export default function WarningReview({ groupId, preview, notify }: Props) {
  const [target, setTarget] = useState(preview ? '456' : '');
  const [review, setReview] = useState<Review | null>(null);
  const [selected, setSelected] = useState<WarningRecord | null>(null);
  const [reason, setReason] = useState('');
  const [loading, setLoading] = useState(false);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState('');
  // Invalidate late reads and mutation notifications on target changes/unmount.
  const version = useRef(0);
  const mutationInFlight = useRef(false);
  const confirmationRef = useRef<HTMLDivElement>(null);
  const openerRef = useRef<HTMLButtonElement>(null);
  useEffect(() => () => { version.current += 1; }, []);
  useEffect(() => { if (selected) confirmationRef.current?.focus(); }, [selected]);

  function changeTarget(value: string) {
    version.current += 1;
    setTarget(value); setReview(null); setSelected(null); setReason('');
    setLoading(false); setError('');
  }

  async function load(targetId: number, before?: string) {
    const current = ++version.current;
    setReview(null); setSelected(null); setReason(''); setError(''); setLoading(true);
    const query = new URLSearchParams({ limit: '5', ...(before ? { before } : {}) });
    try {
      const page = await api.get<WarningPage>(`/groups/${groupId}/members/${targetId}/warnings?${query}`);
      if (current === version.current) setReview({ targetId, page, first: !before });
    } catch (err) {
      if (current === version.current) setError(err instanceof Error ? err.message : 'Could not load warnings.');
    } finally {
      if (current === version.current) setLoading(false);
    }
  }

  function find(event: React.FormEvent) {
    event.preventDefault();
    const targetId = Number(target);
    if (!/^\d+$/.test(target) || !Number.isSafeInteger(targetId) || targetId <= 0) {
      setError('Enter a valid Telegram user ID.');
      return;
    }
    void load(targetId);
  }

  async function confirm() {
    if (!review || !selected || preview || busy || loading || mutationInFlight.current) return;
    const { targetId } = review;
    const warningId = selected.id;
    const current = ++version.current;
    mutationInFlight.current = true;
    setBusy(true); setError('');
    try {
      await api.post(`/groups/${groupId}/members/${targetId}/warnings/${warningId}/revoke`, { reason });
      if (current !== version.current) return;
      setBusy(false); setSelected(null); setReview(null); setReason('');
      notify('Warning revoked.');
      // A read is safe after confirmed success; never repeat the mutation.
      void load(targetId);
    } catch {
      if (current !== version.current) return;
      setBusy(false); setSelected(null); setReview(null); setReason('');
      setError('Could not confirm the revocation. Review warnings before trying again.');
    } finally {
      mutationInFlight.current = false;
    }
  }

  return <section className="page-card group-panel warning-review" aria-labelledby="warning-review-title">
    <div className="panel-title"><div><span className="eyebrow">A FAIRER RECORD</span><h3 id="warning-review-title">Review warnings</h3><p>Look up a member's active warnings. Revocation corrects SUMMON's record; it does not change Telegram restrictions.</p></div></div>
    {preview && <p className="field-hint">Sample data only · member 456 has an example warning. Revocation is disabled.</p>}
    <form className="warning-search" onSubmit={find}>
      <label>Member ID to review<input inputMode="numeric" maxLength={16} value={target} onChange={event => changeTarget(event.target.value)} disabled={busy} placeholder="Member's numeric ID" /></label>
      <button className="button button-subtle" type="submit" disabled={busy || loading}>Find warnings</button>
    </form>
    {loading && <p className="muted-small" role="status">Loading warnings…</p>}
    {error && <p className="warning-error" role="alert">{error}</p>}
    {review && <div className="warning-results">
      <p className="muted-small" aria-live="polite">{review.page.active_count} active {review.page.active_count === 1 ? 'warning' : 'warnings'} for member {review.targetId}. Counts can change as admins act.</p>
      {!review.page.items.length ? <p className="muted-small">{review.page.active_count ? 'No warnings on this page; return to the first page.' : 'No active warnings for this member.'}</p> : <ul className="warning-list">{review.page.items.map(item => <li className="warning-record" key={item.id}>
        <div><strong>{item.reason || 'No reason provided.'}</strong><small>Issued by {item.actor_id} · {new Date(item.created_at).toLocaleString()}</small><code>{item.id}</code></div>
        <button className="button button-subtle" type="button" disabled={preview || busy} onClick={event => { openerRef.current = event.currentTarget; setSelected(item); setReason(''); }}>Revoke warning</button>
      </li>)}</ul>}
      {(review.page.next_cursor || !review.first) && <div className="warning-pagination">
        {!review.first && <button className="text-action" type="button" disabled={busy} onClick={() => void load(review.targetId)}>First page</button>}
        {review.page.next_cursor && <button className="text-action" type="button" disabled={busy} onClick={() => void load(review.targetId, review.page.next_cursor || undefined)}>Older warnings</button>}
      </div>}
      {selected && <div ref={confirmationRef} tabIndex={-1} className="warning-confirm" role="region" aria-label="Confirm warning revocation">
        <h4>Confirm revocation</h4>
        <p>Revoke warning {selected.id} for member {review.targetId}? The warning and your correction remain in the audit record.</p>
        <label>Reason for revocation (optional)<textarea maxLength={500} value={reason} disabled={busy} onChange={event => setReason(event.target.value)} /></label>
        <div className="warning-confirm-actions"><button className="button button-subtle" type="button" disabled={busy} onClick={() => { setSelected(null); setReason(''); openerRef.current?.focus(); }}>Cancel</button><button className="button button-primary" type="button" disabled={busy} onClick={() => void confirm()}>{busy ? 'Confirming…' : 'Confirm revocation'}</button></div>
      </div>}
    </div>}
  </section>;
}
