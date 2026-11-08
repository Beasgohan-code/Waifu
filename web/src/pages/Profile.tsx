import { useState } from 'react';
import { BookOpen, Check, Copy, Flame, Heart, Layers, Sparkles, Star, Trophy, Zap } from 'lucide-react';
import { ProgressBar } from '../components';
import { useRemote } from '../hooks';
import { rarityLabel } from '../rarities';
import type { CollectionInsights, Profile as Player, View } from '../types';

export default function Profile({ profile, navigate, revision }: {
  profile: Player; navigate: (view: View) => void; revision: number;
}) {
  const [retry, setRetry] = useState(0);
  const { data: insights, loading, error } = useRemote<CollectionInsights>(
    '/me/collection-insights', revision + retry,
  );
  return <div className="page-content">
    <div className="page-heading"><span className="eyebrow">THE PERSON BEHIND THE COLLECTION</span><h1>Collector profile.</h1><p>A little space for your journey through SUMMON.</p></div>
    <div className="profile-hero"><div className="profile-avatar">{profile.first_name.charAt(0).toUpperCase()}<span>✦</span></div><div><span className="eyebrow">COLLECTOR · LEVEL {profile.level}</span><h2>{profile.first_name}</h2><p>{profile.username ? `@${profile.username}` : 'Your story is just getting started.'}</p></div><span className="profile-ornament" aria-hidden="true">✧</span></div>
    <div className="profile-stats" aria-label="Your collector statistics">
      <div className="page-card"><BookOpen size={19} /><span>Unique characters</span><strong>{(insights?.unique_characters ?? profile.unique_characters).toLocaleString()}</strong></div>
      <div className="page-card"><Sparkles size={19} /><span>Group catches</span><strong>{profile.catch_count.toLocaleString()}</strong></div>
      <div className="page-card"><Star size={19} /><span>Progress points</span><strong>{profile.points.toLocaleString()}</strong></div>
      <div className="page-card"><Flame size={19} /><span>Last recorded streak</span><strong>{profile.streak} days</strong><small>Best recorded: {profile.best_streak ?? profile.streak} days</small></div>
    </div>
    <div className="profile-panels">
      <div className="page-card profile-progress"><span className="eyebrow">YOUR PROGRESS</span><h2>Every discovery counts.</h2>
        {loading ? <p>Loading collection progress…</p> : error ? <p>Collection progress is temporarily unavailable.</p> : insights && <>
          {insights.catalog_size ? <><p>You own {insights.active_owned} of {insights.catalog_size} active catalogue characters · {insights.completion_percent}% complete.</p><ProgressBar value={insights.active_owned} max={insights.catalog_size} label="Active catalogue completion" /></> : <p>There are no active catalogue characters yet.</p>}
        </>}
        <button className="text-action" onClick={() => navigate('collection')}>Visit your collection →</button>
      </div>
      <div className="page-card profile-progress"><span className="eyebrow">COLLECTOR LEVEL</span><h2><Zap size={22} /> Level {profile.level}</h2><p>{profile.xp.toLocaleString()} total XP earned by collecting and checking in.</p><button className="text-action" onClick={() => navigate('progress')}><Trophy size={15} /> View achievements →</button></div>
    </div>
    <section className="insights-section" aria-labelledby="insights-heading" aria-busy={loading}>
      <div className="insights-heading"><div><span className="eyebrow">YOUR COLLECTION, AT A GLANCE</span><h2 id="insights-heading">Collector insights.</h2><p>Private to you · based on your current collection.</p></div><span className="insights-heading-icon" aria-hidden="true"><Layers size={22} /></span></div>
      {loading ? <div className="loading-line" role="status">Loading collector insights…</div> : error ? <div className="page-card insights-error" role="alert"><p>We couldn't load your collection insights. Your collection is unchanged.</p><button className="button button-small" onClick={() => setRetry(value => value + 1)}>Try again</button></div> : insights && <>
        <div className="insight-stats">
          <div className="page-card"><Layers size={18} /><span>Copies owned</span><strong>{insights.total_copies.toLocaleString()}</strong></div>
          <div className="page-card"><Copy size={18} /><span>Extra copies</span><strong>{insights.duplicate_copies.toLocaleString()}</strong></div>
          <div className="page-card"><Heart size={18} /><span>Favorites</span><strong>{insights.favorite_characters.toLocaleString()}</strong></div>
        </div>
        <div className="insights-columns">
          <div className="page-card insights-panel"><span className="eyebrow">THE STORIES YOU HOLD</span><h3>Rarity breakdown</h3><p>Includes owned characters that may no longer be active in the catalogue.</p>
            {insights.rarity_breakdown.length ? <ul className="rarity-breakdown">{insights.rarity_breakdown.map(item => <li key={item.rarity}><span>{item.rarity === 'unknown' ? 'Uncategorized' : rarityLabel(item.rarity)}</span><strong>{item.unique.toLocaleString()} unique</strong><small>{item.copies.toLocaleString()} copies</small></li>)}</ul> : <p>Your collection is empty. Your rarity breakdown will appear here as you collect.</p>}
          </div>
          <div className="page-card insights-panel"><span className="eyebrow">LITTLE STEPS, BIG STORIES</span><h3>Collection goals</h3><p>These reflect what you own now and can change after a trade or gift; they are not permanent rewards.</p>
            <ul className="collection-goals">{insights.goals.map(goal => <li key={goal.id}><span className={goal.completed ? 'goal-icon completed' : 'goal-icon'} aria-hidden="true">{goal.completed ? <Check size={15} /> : <Star size={15} />}</span><span><strong>{goal.title}</strong><small>{goal.completed ? 'Current goal complete' : `${goal.current} / ${goal.target} collected`}</small></span><span className="goal-status">{goal.completed ? 'Complete' : `${goal.current}/${goal.target}`}</span></li>)}</ul>
          </div>
        </div>
      </>}
    </section>
  </div>;
}
