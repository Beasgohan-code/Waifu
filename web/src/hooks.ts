import { useEffect, useState } from 'react';
import { api } from './api';
import type { Progression } from './types';

export function useRemote<T>(path: string | null, revision = 0) {
  const [data, setData] = useState<T | null>(null);
  const [loading, setLoading] = useState(Boolean(path));
  const [error, setError] = useState('');
  useEffect(() => {
    let active = true;
    if (!path) { setData(null); setLoading(false); return; }
    setLoading(true); setError('');
    api.get<T>(path).then(value => { if (active) setData(value); })
      .catch((err: Error) => { if (active) { setError(err.message); setData(null); } })
      .finally(() => { if (active) setLoading(false); });
    return () => { active = false; };
  }, [path, revision]);
  return { data, loading, error };
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
