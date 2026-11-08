import { rarities } from '../rarities';
import { ArrowLeft, ArrowRight, RefreshCw, Search, SlidersHorizontal } from 'lucide-react';
import { useEffect, useState } from 'react';
import { CharacterCard, EmptyState, SectionTitle, Skeleton } from '../components';
import { useRemote } from '../hooks';
import type { Character, Page, Rarity } from '../types';

interface Props { mode: 'catalog' | 'collection'; preview: boolean; revision: number; onOpen: (id: string) => void; onFavorite: (character: Character) => void; onDiscover: () => void }
type CollectionFilter = 'all' | 'favorites' | 'duplicates';
type CollectionSort = 'recent' | 'copies';
type CollectionPage = Page<Character> & { page_cursor?: string | null; next_cursor?: string | null };

export default function Catalog({ mode, preview, revision, onOpen, onFavorite, onDiscover }: Props) {
  const [search, setSearch] = useState('');
  const [query, setQuery] = useState('');
  const [rarity, setRarity] = useState<Rarity | 'all'>('all');
  const [filter, setFilter] = useState<CollectionFilter>('all');
  const [sort, setSort] = useState<CollectionSort>('recent');
  const [page, setPage] = useState(1);
  const [cursors, setCursors] = useState<Array<string | null>>([null]);
  const [standardPages, setStandardPages] = useState(false);
  const [refreshRevision, setRefreshRevision] = useState(0);

  const resetPages = () => { setPage(1); setCursors([null]); };
  const refreshCollection = () => { resetPages(); setRefreshRevision(value => value + 1); };
  useEffect(() => {
    const next = search.trim();
    if (mode !== 'catalog' || next === query) return;
    const timer = window.setTimeout(() => { setQuery(next); resetPages(); }, 250);
    return () => window.clearTimeout(timer);
  }, [mode, search, query]);
  // Favorites and transfers can change both membership and ordering: start a new snapshot.
  useEffect(() => { resetPages(); }, [mode, revision]);

  const snapshotPages = mode === 'collection' && !standardPages;
  const params = new URLSearchParams();
  if (snapshotPages) {
    const cursor = cursors[page - 1];
    if (cursor) params.set('cursor', cursor);
  } else params.set('page', String(page));
  params.set('limit', '24');
  if (mode === 'catalog' && query) params.set('search', query);
  if (mode === 'catalog' && rarity !== 'all') params.set('rarity', rarity);
  if (mode === 'collection' && filter === 'favorites') params.set('favorite', 'true');
  if (mode === 'collection' && filter === 'duplicates') params.set('duplicates', 'true');
  if (mode === 'collection' && sort !== 'recent') params.set('sort', sort);
  const endpoint = mode === 'catalog' ? 'characters' : snapshotPages ? 'inventory/pages' : 'inventory';
  const { data, loading, error } = useRemote<CollectionPage>(`/${endpoint}?${params}`, revision + refreshRevision);
  const chooseFilter = (next: CollectionFilter) => { setFilter(next); resetPages(); };
  const nextPage = () => {
    if (snapshotPages) {
      if (!data?.next_cursor) return;
      const nextCursor = data.next_cursor;
      setCursors(previous => {
        const next = previous.slice(0, page);
        // Replaying page one must use the same snapshot, not silently start a new one.
        if (page === 1 && data.page_cursor) next[0] = data.page_cursor;
        next[page] = nextCursor;
        return next;
      });
    }
    setPage(value => value + 1);
  };
  const hasPages = Boolean(data && (snapshotPages ? data.next_cursor || page > 1 : data.total > data.limit));
  const nextDisabled = loading || !data || (snapshotPages ? !data.next_cursor : page * data.limit >= data.total || page >= 25);
  const sectionTitle = mode === 'catalog' ? 'Character catalogue' : filter === 'favorites' ? 'Favorite characters' : filter === 'duplicates' ? 'Duplicate characters' : 'All companions';
  const emptyTitle = mode === 'catalog' ? 'No characters found' : filter === 'favorites' ? 'No favorites yet' : filter === 'duplicates' ? 'No duplicates yet' : 'Your collection is just beginning';
  const emptyText = mode === 'catalog' ? 'Try another name or rarity filter.' : filter === 'favorites' ? 'Favorite an owned character to see them here.' : filter === 'duplicates' ? 'Extra copies will appear here when you collect the same character again.' : 'Catch a character in a group to add them to your collection.';
  return <div className="page-content">
    <div className="page-heading"><span className="eyebrow">{mode === 'catalog' ? 'THE SUMMON ARCHIVE' : 'YOUR LITTLE UNIVERSE'}</span>
      <h1>{mode === 'catalog' ? 'Meet the characters.' : 'Your collection.'}</h1>
      <p>{mode === 'catalog' ? 'Find a new favorite among the stories waiting to be discovered.' : 'Every companion you have met, all in one place.'}</p></div>
    <div className="catalog-toolbar">
      {mode === 'catalog' ? <label className="search-field"><Search size={18} /><span className="sr-only">Search characters by name</span><input value={search} onChange={event => setSearch(event.target.value)} placeholder="Search characters by name..." /></label>
        : <div className="segment collection-filters" role="group" aria-label="Collection filter">
          <button className={filter === 'all' ? 'active' : ''} aria-pressed={filter === 'all'} onClick={() => chooseFilter('all')}>All characters</button>
          <button className={filter === 'favorites' ? 'active' : ''} aria-pressed={filter === 'favorites'} onClick={() => chooseFilter('favorites')}>Favorites</button>
          <button className={filter === 'duplicates' ? 'active' : ''} aria-pressed={filter === 'duplicates'} onClick={() => chooseFilter('duplicates')}>Duplicates</button>
        </div>}
      {mode === 'catalog' ? <label className="select-field"><SlidersHorizontal size={16} /><span className="sr-only">Filter by rarity</span><select value={rarity} onChange={event => { setRarity(event.target.value as Rarity | 'all'); resetPages(); }}><option value="all">All rarities</option>{rarities.map(item => <option key={item.value} value={item.value}>{item.label}</option>)}</select></label>
        : <label className="select-field"><SlidersHorizontal size={16} /><span className="sr-only">Sort collection</span><select value={sort} onChange={event => { setSort(event.target.value as CollectionSort); resetPages(); }}><option value="recent">First added (newest)</option><option value="copies">Most copies</option></select></label>}
    </div>
    <div className="catalog-section"><SectionTitle eyebrow={mode === 'catalog' ? 'EXPLORE THE ARCHIVE' : 'COLLECTED WITH CARE'} title={sectionTitle} />
      {mode === 'collection' && !error && <div className="collection-page-note">
        <span>{standardPages ? 'Standard pages are limited to the first 600 characters and may shift as your collection changes.' : 'Pages show a short-lived snapshot. Refresh to see new changes.'}</span>
        <button className="text-action" onClick={refreshCollection}><RefreshCw size={14} /> Refresh collection</button>
      </div>}
      {!loading && data && <div className="result-count">{data.total} {data.total === 1 ? 'character' : 'characters'}</div>}
      {loading ? <Skeleton count={4} /> : error ? <>
        <EmptyState title="Couldn't load characters" text={error} action={mode === 'collection' ? 'Refresh collection' : undefined} onAction={refreshCollection} />
        {snapshotPages && <div className="collection-fallback"><button className="button button-subtle" onClick={() => { setStandardPages(true); resetPages(); }}>Use standard pages (first 600)</button></div>}
      </> : data?.items.length ? <div className="character-grid four">{data.items.map(character => <CharacterCard key={character.id} character={character} onOpen={onOpen} onFavorite={onFavorite} preview={preview} />)}</div> :
        <EmptyState title={emptyTitle} text={emptyText} action={mode === 'collection' && filter === 'all' ? 'Explore characters' : undefined} onAction={onDiscover} />}
      {data && hasPages && <div className="pagination"><button className="icon-button" aria-label="Previous page" disabled={loading || page === 1} onClick={() => setPage(value => value - 1)}><ArrowLeft size={17} /></button><span>Page {page}</span><button className="icon-button" aria-label="Next page" disabled={nextDisabled} onClick={nextPage}><ArrowRight size={17} /></button></div>}
    </div>
  </div>;
}
