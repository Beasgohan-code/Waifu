import { ArrowRight, Check, Gift, Sparkles, Trophy } from 'lucide-react';
import { EmptyState, ProgressBar, SectionTitle } from '../components';
import { useProgression } from '../hooks';

export default function Progress({ preview, revision, daily, dailyBusy }: { preview: boolean; revision: number; daily: () => void; dailyBusy: boolean }) {
  const { data, loading, error } = useProgression(revision);
  const status = data?.daily_status?.state;
  return <div className="page-content"><div className="page-heading"><span className="eyebrow">LITTLE MOMENTS, BIG STORIES</span><h1>Your journey.</h1><p>Celebrate the milestones that make your collection yours.</p></div>
    {loading ? <div className="loading-line">Loading your journey…</div> : error ? <EmptyState title="Couldn't load your journey" text={error} /> : data && <>
      <SectionTitle eyebrow="A FRESH START" title="Today's little quests" />
      <div className="quest-grid">{data.daily.map(quest => <div className="page-card quest-card" key={quest.id}><span className="large-icon">{quest.id === 'check-in' ? <Gift size={20} /> : <Sparkles size={20} />}</span><div><h3>{quest.title}</h3><p>{quest.description}</p><span className="reward-tag">✦ {quest.reward}</span></div>
        {quest.id === 'check-in' ? <button className="button button-small" disabled={preview || dailyBusy || status !== 'available'} onClick={daily}>{preview ? 'Preview' : dailyBusy ? 'Claiming…' : status === 'claimed' ? 'Claimed' : status === 'available' ? 'Claim' : 'Unavailable'}{!preview && !dailyBusy && status === 'available' && <ArrowRight size={14} />}</button> : <span className={`status-pill ${quest.completed ? 'status-completed' : ''}`}>{quest.completed ? 'Complete' : 'In progress'}</span>}
      </div>)}</div>
      <div className="spaced-section"><SectionTitle eyebrow="COLLECTION MILESTONES" title="Achievements" /><div className="achievement-grid">{data.achievements.map(item => <div className={`page-card achievement-card ${item.completed ? 'achieved' : ''}`} key={item.id}><span className="achievement-medal">{item.completed ? <Check size={19} /> : <Trophy size={19} />}</span><div className="achievement-body"><div className="achievement-heading"><strong>{item.title}</strong><span>{item.completed ? 'UNLOCKED' : `${item.current} / ${item.target}`}</span></div><p>{item.description}</p><ProgressBar value={item.current} max={item.target} label={item.title} /></div></div>)}</div></div>
    </>}
  </div>;
}
