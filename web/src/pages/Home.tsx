import { ArrowRight, BookOpen, Gift, ShieldCheck, Sparkles, Star, Trophy, Zap } from 'lucide-react';
import { CharacterCard, EmptyState, ProgressBar, SectionTitle, Skeleton } from '../components';
import { useProgression, useRemote } from '../hooks';
import type { Character, Page, Profile, View } from '../types';

interface Props {
  profile: Profile; preview: boolean; revision: number; navigate: (view: View) => void;
  openCharacter: (id: string) => void; favorite: (character: Character) => void;
  daily: () => void; dailyBusy: boolean;
}

export default function Home({ profile, preview, revision, navigate, openCharacter, favorite, daily, dailyBusy }: Props) {
  const { data: catalogue, loading, error } = useRemote<Page<Character>>('/characters?limit=3', revision);
  const { data: progression, loading: checkingStatus, refresh: refreshStatus } = useProgression(revision);
  const status = progression?.daily_status?.state;
  const canClaim = !preview && !dailyBusy && !checkingStatus && status === 'available';
  const dailyLabel = preview ? 'Preview only' : dailyBusy ? 'Claiming…' : checkingStatus ? 'Checking daily status…'
    : status === 'claimed' ? 'Claimed today' : status === 'available' ? 'Claim daily reward' : 'Daily unavailable';
  const nextAchievement = progression?.achievements.find(item => !item.completed);
  return <div className="page-content home-page">
    <div className="page-heading home-heading"><div><span className="eyebrow">YOUR COLLECTOR'S SPACE <span className="eyebrow-star">✦</span></span>
      <h1>Welcome back, <em>{profile.first_name}.</em></h1><p>Your next favorite story is waiting to be discovered.</p></div>
      <div className="heading-date"><span className="tiny-spark">✧</span> YOUR ADVENTURE AWAITS</div>
    </div>

    <section className="hero-card" aria-labelledby="hero-title">
      <div className="hero-image" aria-hidden="true" />
      <div className="hero-content"><div className="hero-kicker"><span className="kicker-icon"><Sparkles size={13} /></span> THE SUMMON UNIVERSE <span className="hero-line" /> VOL. 01</div>
        <h2 id="hero-title">A little magic,<br /><em>for every collector.</em></h2>
        <p>Discover extraordinary characters, build a collection that's yours, and share the adventure with friends.</p>
        <div className="hero-actions"><button className="button button-primary" onClick={() => navigate('catalog')}>Explore characters <ArrowRight size={16} /></button>
          <button className="button button-ghost" onClick={() => navigate('collection')}>My collection</button></div>
      </div>
      <div className="hero-caption"><span>✦</span><div>MEET YOUR NEXT<br /><strong>COMPANION</strong></div></div>
    </section>

    <div className="overview-stats" aria-label="Your collector statistics">
      <div><span className="stat-icon lilac"><BookOpen size={17} /></span><p><small>YOUR COLLECTION</small><strong>{profile.unique_characters}<i> / {profile.catalog_size}</i></strong></p></div>
      <div><span className="stat-icon peach"><Sparkles size={17} /></span><p><small>CHARACTERS CAUGHT</small><strong>{profile.catch_count}</strong></p></div>
      <div><span className="stat-icon gold"><Star size={17} /></span><p><small>YOUR POINTS</small><strong>{profile.points.toLocaleString()}</strong></p></div>
      <div><span className="stat-icon blue"><Zap size={17} /></span><p><small>COLLECTOR LEVEL</small><strong>{profile.level}<i> / onward</i></strong></p></div>
    </div>

    <div className="home-columns"><div className="home-main">
      <SectionTitle eyebrow="THE CHARACTERS" title="A world worth collecting" action="View catalogue" onAction={() => navigate('catalog')} />
      {loading ? <Skeleton /> : error ? <EmptyState title="Couldn't load the catalogue" text={error} /> : catalogue?.items.length ?
        <div className="character-grid">{catalogue.items.map(character => <CharacterCard key={character.id} character={character} onOpen={openCharacter} onFavorite={favorite} preview={preview} />)}</div> :
        <EmptyState title="Your story starts here" text="The catalogue is waiting for its first characters." />}
      <div className="collection-prompt"><div className="prompt-symbol"><Sparkles size={19} /></div><div><strong>Make it your own.</strong><span>Every character has a story. Start collecting yours.</span></div><button onClick={() => navigate('collection')} aria-label="Open my collection"><ArrowRight size={18} /></button></div>
    </div>
    <aside className="home-rail" aria-label="Your journey">
      <div className="rail-heading"><span className="eyebrow">A LITTLE EVERY DAY</span><h2>Your journey</h2></div>
      <div className="journey-card"><div className="journey-top"><span className="journey-icon"><Gift size={19} /></span><span className="soft-label">DAILY REWARD</span></div>
        <h3>A little something<br />for you.</h3><p>Check in daily and keep your collector streak going.</p>
        <div className="reward-line"><span><Sparkles size={15} /> 100+ points</span><small>RESETS 00:00 UTC</small></div>
        <button className="button button-primary full" disabled={!canClaim} onClick={daily}>{dailyLabel}{canClaim && <ArrowRight size={15} />}</button>
        {!preview && !checkingStatus && (!status || status === 'unavailable') && <button className="rail-link" onClick={refreshStatus}>Refresh daily status</button>}
      </div>
      <div className="journey-card progress-card"><div className="mini-label"><span>YOUR COLLECTION</span><BookOpen size={16} /></div>
        <div className="progress-number">{profile.unique_characters}<span> / {profile.catalog_size}</span></div><p>characters discovered</p>
        <ProgressBar value={profile.unique_characters} max={profile.catalog_size} label="Collection progress" />
        <button className="rail-link" onClick={() => navigate('collection')}>See your collection <ArrowRight size={14} /></button>
      </div>
      <button className="achievement-teaser" onClick={() => navigate('progress')}><span className="achievement-icon"><Trophy size={17} /></span><span><small>YOUR NEXT MILESTONE</small><strong>{nextAchievement?.title || 'View your achievements'}</strong></span><ArrowRight size={15} /></button>
    </aside></div>
    <div className="community-strip"><span className="community-icon"><ShieldCheck size={19} /></span><div><strong>Better together.</strong><span>Bring SUMMON to your group and build your own little universe.</span></div><button className="text-action" onClick={() => navigate('groups')}>Explore groups <ArrowRight size={15} /></button></div>
  </div>;
}
