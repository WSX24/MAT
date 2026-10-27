import { createContext, useCallback, useContext, useEffect, useMemo, useRef, useState, type ReactNode } from 'react';
import { IconClose } from './icons';

export type ToastLevel = 'info' | 'ok' | 'warn' | 'error';

interface ToastItem {
  id: number;
  level: ToastLevel;
  message: string;
}

interface ToastApi {
  push: (level: ToastLevel, message: string) => void;
  info: (message: string) => void;
  ok: (message: string) => void;
  warn: (message: string) => void;
  error: (message: string) => void;
}

const ToastContext = createContext<ToastApi | null>(null);

export function ToastProvider({ children }: { children: ReactNode }) {
  const [items, setItems] = useState<ToastItem[]>([]);
  const seq = useRef(0);
  const timers = useRef<number[]>([]);

  const remove = useCallback((id: number) => {
    setItems((current) => current.filter((item) => item.id !== id));
  }, []);

  const push = useCallback(
    (level: ToastLevel, message: string) => {
      seq.current += 1;
      const id = seq.current;
      setItems((current) => [...current.slice(-3), { id, level, message }]);
      const timer = window.setTimeout(() => remove(id), level === 'error' ? 7000 : 4200);
      timers.current.push(timer);
    },
    [remove],
  );

  useEffect(() => {
    const list = timers.current;
    return () => list.forEach((timer) => window.clearTimeout(timer));
  }, []);

  const api = useMemo<ToastApi>(
    () => ({
      push,
      info: (message) => push('info', message),
      ok: (message) => push('ok', message),
      warn: (message) => push('warn', message),
      error: (message) => push('error', message),
    }),
    [push],
  );

  return (
    <ToastContext.Provider value={api}>
      {children}
      <div className="toast-region" aria-live="polite" aria-atomic="false">
        {items.map((item) => (
          <div key={item.id} className={'toast toast--' + item.level} role={item.level === 'error' ? 'alert' : 'status'}>
            <div className="toast__body">{item.message}</div>
            <button type="button" className="toast__close" aria-label="关闭提示" onClick={() => remove(item.id)}>
              <IconClose size={16} />
            </button>
          </div>
        ))}
      </div>
    </ToastContext.Provider>
  );
}

export function useToast(): ToastApi {
  const api = useContext(ToastContext);
  if (!api) throw new Error('useToast 必须在 ToastProvider 内使用');
  return api;
}
