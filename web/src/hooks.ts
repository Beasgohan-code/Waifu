import { useEffect, useState } from 'react';
import { api } from './api';

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
