import { BookOpen, Flame, Sparkles, Star, Trophy, Zap } from 'lucide-react';
import { ProgressBar } from '../components';
import type { Profile as Player, View } from '../types';

export default function Profile({ profile, navigate }: { profile: Player; navigate: (view: View) => void }) {
  return <div className="page-content"><div className="page-heading"><span className="eyebrow">THE PERSON BEHIND THE COLLECTION</span><h1>Collector profile.</h1><p>A little space for your journey through SUMMON.</p></div>
    <div className="profile-hero"><div className="profile-avatar">{profile.first_name.charAt(0).toUpperCase()}<span>✦</span></div><div><span className="eyebrow">COLLECTOR · LEVEL {profile.level}</span><h2>{profile.first_name}</h2><p>{profile.username ? `@${profile.username}` : 'Your story is just getting started.'}</p></div><span className="profile-ornament">✧</span></div>
    <div className="profile-stats"><div className="page-card"><BookOpen size={19} /><span>Unique characters</span><strong>{profile.unique_characters}</strong></div><div className="page-card"><Sparkles size={19} /><span>Total summons</span><strong>{profile.catch_count}</strong></div><div className="page-card"><Star size={19} /><span>Progress points</span><strong>{profile.points.toLocaleString()}</strong></div><div className="page-card"><Flame size={19} /><span>Check-in streak</span><strong>{profile.streak} days</strong></div></div>
    <div className="profile-panels"><div className="page-card profile-progress"><span className="eyebrow">YOUR PROGRESS</span><h2>Every discovery counts.</h2><p>You have discovered {profile.unique_characters} of {profile.catalog_size} characters in the catalogue.</p><ProgressBar value={profile.unique_characters} max={profile.catalog_size} label="Collection completion" /><button className="text-action" onClick={() => navigate('collection')}>Visit your collection →</button></div>
      <div className="page-card profile-progress"><span className="eyebrow">COLLECTOR LEVEL</span><h2><Zap size={22} /> Level {profile.level}</h2><p>{profile.xp.toLocaleString()} total XP earned by collecting and checking in.</p><button className="text-action" onClick={() => navigate('progress')}><Trophy size={15} /> View achievements →</button></div></div>
  </div>;
}
