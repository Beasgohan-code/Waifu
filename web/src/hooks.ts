import { useEffect, useState } from 'react';
import { api } from './api';
import type { Progression } from './types';

type RemoteState<T> = { path: string | null; revision: number; data: T | null; loading: boolean; error: string };

export function useRemote<T>(path: string | null, revision = 0) {
  const [state, setState] = useState<RemoteState<T>>({ path, revision, data: null, loading: Boolean(path), error: '' });
  useEffect(() => {
    let active = true;
    if (!path) { setState({ path, revision, data: null, loading: false, error: '' }); return; }
    setState({ path, revision, data: null, loading: true, error: '' });
    api.get<T>(path)
      .then(data => { if (active) setState({ path, revision, data, loading: false, error: '' }); })
      .catch((err: Error) => { if (active) setState({ path, revision, data: null, loading: false, error: err.message }); });
    return () => { active = false; };
  }, [path, revision]);
  // The effect runs after rendering. Never show an old group's data, errors or
  // enabled controls during the render where the path/revision has changed.
  if (state.path !== path || state.revision !== revision) return { data: null, loading: Boolean(path), error: '' };
  return { data: state.data, loading: state.loading, error: state.error };
}

// Refresh after a UTC rollover or when a suspended Mini App becomes visible.
// Relative timers only trigger a server read; the device clock never grants eligibility.
export function useProgression(revision: number) {
  const [refreshRevision, setRefreshRevision] = useState(0);
  const refresh = () => setRefreshRevision(value => value + 1);
  useEffect(() => {
    const timer = window.setInterval(refresh, 60_000);
    const onVisible = () => { if (document.visibilityState === 'visible') refresh(); };
    document.addEventListener('visibilitychange', onVisible);
    return () => { window.clearInterval(timer); document.removeEventListener('visibilitychange', onVisible); };
  }, []);
  return { ...useRemote<Progression>('/progression', revision + refreshRevision), refresh };
}
