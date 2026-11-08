import { Activity, CheckCircle2, Clock3, Sparkles } from 'lucide-react';
import { useState } from 'react';
import { useRemote } from '../hooks';
import type { GroupSummonInsights as Insights } from '../types';

function valid(data: Insights): boolean {
  const values = [data.attempts, data.caught, data.expired, data.delivery_rejected, data.awaiting_resolution, data.my_catches];
  return data.window_days === 30 && values.every(value => Number.isSafeInteger(value) && value >= 0)
    && data.attempts === data.caught + data.expired + data.delivery_rejected + data.awaiting_resolution
    && data.my_catches <= data.caught && Number.isFinite(Date.parse(data.as_of));
}

export default function GroupSummonInsights({ groupId, revision }: { groupId: number; revision: number }) {
  const [retry, setRetry] = useState(0);
  const { data, loading, error } = useRemote<Insights>(`/groups/${groupId}/summon-insights`, revision + retry);
  return <section className="page-card group-panel" aria-labelledby="group-summon-insights-heading">
    <div className="panel-title"><div><span className="eyebrow">YOUR COMMUNITY'S SUMMONS</span><h3 id="group-summon-insights-heading">Summon insights</h3><p>A private, rolling 30-day view of recorded activity in this group.</p></div><Activity size={20} /></div>
    {loading ? <p className="loading-line" role="status">Loading summon insights…</p> : error || !data || !valid(data) ? <div role="alert"><p>Summon insights are unavailable right now.</p><button type="button" className="button button-subtle" onClick={() => setRetry(value => value + 1)}>Try again</button></div> : <>
      <div className="group-stat-grid summon-insight-grid">
        <div className="page-card"><Sparkles size={18} /><span>Recorded attempts</span><strong>{data.attempts.toLocaleString()}</strong></div>
        <div className="page-card"><CheckCircle2 size={18} /><span>Caught</span><strong>{data.caught.toLocaleString()}</strong></div>
        <div className="page-card"><Activity size={18} /><span>Your catches here</span><strong>{data.my_catches.toLocaleString()}</strong></div>
        <div className="page-card"><Clock3 size={18} /><span>Expired</span><strong>{data.expired.toLocaleString()}</strong></div>
        <div className="page-card"><Clock3 size={18} /><span>Awaiting resolution</span><strong>{data.awaiting_resolution.toLocaleString()}</strong></div>
        <div className="page-card"><Activity size={18} /><span>Delivery rejected</span><strong>{data.delivery_rejected.toLocaleString()}</strong></div>
      </div>
      <p className="muted-small">As of <time dateTime={data.as_of}>{new Date(data.as_of).toUTCString()}</time>. Attempts are reservations and may not have been delivered; older or missing records are not included. No other member identities or rankings are shown.</p>
    </>}
  </section>;
}
