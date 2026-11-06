import { ArrowLeft, ArrowRight, Search, SlidersHorizontal } from 'lucide-react';
import { useEffect, useState } from 'react';
import { CharacterCard, EmptyState, SectionTitle, Skeleton } from '../components';
import { useRemote } from '../hooks';
import type { Character, Page, Rarity } from '../types';

interface Props { mode: 'catalog' | 'collection'; preview: boolean; revision: number; onOpen: (id: string) => void; onFavorite: (character: Character) => void; onDiscover: () => void }

export default function Catalog({ mode, preview, revision, onOpen, onFavorite, onDiscover }: Props) {
  const [search, setSearch] = useState('');
  const [query, setQuery] = useState('');
  const [rarity, setRarity] = useState<Rarity | 'all'>('all');
  const [favorites, setFavorites] = useState(false);
  const [page, setPage] = useState(1);
  useEffect(() => { const timer = window.setTimeout(() => { setQuery(search.trim()); setPage(1); }, 250); return () => window.clearTimeout(timer); }, [search]);
  useEffect(() => { setPage(1); }, [mode, rarity, favorites]);
  const params = new URLSearchParams({ page: String(page), limit: '24' });
  if (mode === 'catalog' && query) params.set('search', query);
  if (mode === 'catalog' && rarity !== 'all') params.set('rarity', rarity);
  if (mode === 'collection' && favorites) params.set('favorite', 'true');
  const { data, loading, error } = useRemote<Page<Character>>(`/${mode === 'catalog' ? 'characters' : 'inventory'}?${params}`, revision);
  return <div className="page-content">
    <div className="page-heading"><span className="eyebrow">{mode === 'catalog' ? 'THE SUMMON ARCHIVE' : 'YOUR LITTLE UNIVERSE'}</span>
      <h1>{mode === 'catalog' ? 'Meet the characters.' : 'Your collection.'}</h1>
      <p>{mode === 'catalog' ? 'Find a new favorite among the stories waiting to be discovered.' : 'Every companion you have met, all in one place.'}</p></div>
    <div className="catalog-toolbar">
      {mode === 'catalog' ? <label className="search-field"><Search size={18} /><span className="sr-only">Search characters by name</span><input value={search} onChange={event => setSearch(event.target.value)} placeholder="Search characters by name..." /></label>
        : <div className="segment" aria-label="Collection filter"><button className={!favorites ? 'active' : ''} onClick={() => setFavorites(false)}>All characters</button><button className={favorites ? 'active' : ''} onClick={() => setFavorites(true)}>Favorites</button></div>}
      {mode === 'catalog' && <label className="select-field"><SlidersHorizontal size={16} /><span className="sr-only">Filter by rarity</span><select value={rarity} onChange={event => setRarity(event.target.value as Rarity | 'all')}><option value="all">All rarities</option><option value="common">Common</option><option value="rare">Rare</option><option value="epic">Epic</option><option value="legendary">Legendary</option></select></label>}
    </div>
    <div className="catalog-section"><SectionTitle eyebrow={mode === 'catalog' ? 'EXPLORE THE ARCHIVE' : 'COLLECTED WITH CARE'} title={mode === 'catalog' ? 'Character catalogue' : favorites ? 'Favorite characters' : 'All companions'} />
      {!loading && data && <div className="result-count">{data.total} {data.total === 1 ? 'character' : 'characters'}</div>}
      {loading ? <Skeleton count={4} /> : error ? <EmptyState title="Couldn't load characters" text={error} /> : data?.items.length ? <div className="character-grid four">{data.items.map(character => <CharacterCard key={character.id} character={character} onOpen={onOpen} onFavorite={onFavorite} preview={preview} />)}</div> :
        <EmptyState title={mode === 'collection' ? favorites ? 'No favorites yet' : 'Your collection is just beginning' : 'No characters found'}
          text={mode === 'collection' ? favorites ? 'Favorite an owned character to see them here.' : 'Catch a character in a group to add them to your collection.' : 'Try another name or rarity filter.'}
          action={mode === 'collection' && !favorites ? 'Explore characters' : undefined} onAction={onDiscover} />}
      {data && data.total > data.limit && <div className="pagination"><button className="icon-button" aria-label="Previous page" disabled={page === 1} onClick={() => setPage(page - 1)}><ArrowLeft size={17} /></button><span>Page {page}</span><button className="icon-button" aria-label="Next page" disabled={page * data.limit >= data.total || page >= 25} onClick={() => setPage(page + 1)}><ArrowRight size={17} /></button></div>}
    </div>
  </div>;
}
