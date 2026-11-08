import { Crown, Sparkles, Trophy } from 'lucide-react';
import { EmptyState, SectionTitle } from '../components';
import { useRemote } from '../hooks';
import type { Leader } from '../types';

export default function Leaderboard({ revision }: { revision: number }) {
  const { data, loading, error } = useRemote<Leader[]>('/leaderboard', revision);
  return <div className="page-content"><div className="page-heading"><span className="eyebrow">THE COLLECTOR COMMUNITY</span><h1>Made for sharing.</h1><p>A little recognition for the collectors making their own stories.</p></div>
    <div className="leader-hero"><span className="large-icon"><Trophy size={24} /></span><div><span className="eyebrow">COLLECTORS' LEADERBOARD</span><h2>Every summon has a story.</h2><p>Ranked by characters caught, never by wagers or purchases.</p></div><Sparkles size={34} /></div>
    <div className="spaced-section"><SectionTitle eyebrow="THE COMMUNITY" title="Top collectors" />
      {loading ? <div className="loading-line">Loading collectors…</div> : error ? <EmptyState title="Couldn't load the leaderboard" text={error} /> : !data?.length ? <EmptyState title="A fresh beginning" text="The first collectors will appear here after catching a character." /> : <div className="list-card leaderboard-list">{data.map((leader, index) => <div className="leader-row" key={`${leader.name}-${index}`}><span className={`leader-rank ${index < 3 ? 'top-rank' : ''}`}>{index === 0 ? <Crown size={17} /> : String(index + 1).padStart(2, '0')}</span><span className="leader-avatar">{leader.name.charAt(0).toUpperCase()}</span><div className="list-main"><strong>{leader.name}</strong><small>Collector level {leader.level}</small></div><div className="leader-count"><strong>{leader.catch_count}</strong><small>SUMMONS</small></div></div>)}</div>}
    </div>
  </div>;
}
