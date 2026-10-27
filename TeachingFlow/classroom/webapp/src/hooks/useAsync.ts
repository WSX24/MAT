import { useCallback, useEffect, useRef, useState } from 'react';
import { errorText } from '../lib/format';

export interface AsyncResult<T> {
  status: 'loading' | 'ready' | 'error';
  data: T | null;
  error: string | null;
  reload: () => void;
  setData: (data: T) => void;
}

/** 最小数据加载器：loading / ready / error 三态 + 手动重试。 */
export function useAsync<T>(loader: () => Promise<T>, deps: unknown[]): AsyncResult<T> {
  const [state, setState] = useState<{ status: 'loading' | 'ready' | 'error'; data: T | null; error: string | null }>({
    status: 'loading',
    data: null,
    error: null,
  });
  const [tick, setTick] = useState(0);
  const loaderRef = useRef(loader);
  loaderRef.current = loader;

  useEffect(() => {
    let alive = true;
    setState({ status: 'loading', data: null, error: null });
    loaderRef.current().then(
      (data) => {
        if (alive) setState({ status: 'ready', data, error: null });
      },
      (error: unknown) => {
        if (alive) setState({ status: 'error', data: null, error: errorText(error) });
      },
    );
    return () => {
      alive = false;
    };
    // deps 由调用方给：这里刻意不做展开校验
  }, [...deps, tick]);

  const reload = useCallback(() => setTick((value) => value + 1), []);
  const setData = useCallback((data: T) => setState({ status: 'ready', data, error: null }), []);

  return { ...state, reload, setData };
}
