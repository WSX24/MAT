import type { ReactNode } from 'react';

export type EmptyArt = 'courses' | 'outline' | 'error' | 'search';

export interface EmptyProps {
  title: string;
  description?: ReactNode;
  actions?: ReactNode;
  art?: EmptyArt;
  error?: boolean;
}

function Art({ art }: { art: EmptyArt }) {
  if (art === 'error') {
    return (
      <svg width="72" height="72" viewBox="0 0 72 72" aria-hidden="true" fill="none" stroke="currentColor" strokeWidth="2.4">
        <circle cx="36" cy="36" r="26" strokeDasharray="4 5" />
        <path d="M36 24v16M36 48h.01" strokeLinecap="round" />
      </svg>
    );
  }
  if (art === 'outline') {
    return (
      <svg width="72" height="72" viewBox="0 0 72 72" aria-hidden="true" fill="none" stroke="currentColor" strokeWidth="2.4">
        <rect x="16" y="12" width="40" height="48" rx="5" />
        <path d="M25 26h22M25 36h22M25 46h14" strokeLinecap="round" />
      </svg>
    );
  }
  if (art === 'search') {
    return (
      <svg width="72" height="72" viewBox="0 0 72 72" aria-hidden="true" fill="none" stroke="currentColor" strokeWidth="2.4">
        <circle cx="32" cy="32" r="18" />
        <path d="M46 46l10 10" strokeLinecap="round" />
      </svg>
    );
  }
  return (
    <svg width="80" height="72" viewBox="0 0 80 72" aria-hidden="true" fill="none" stroke="currentColor" strokeWidth="2.4">
      <rect x="12" y="10" width="56" height="40" rx="5" />
      <path d="M12 22h56" />
      <circle cx="20" cy="16" r="1.6" fill="currentColor" />
      <circle cx="27" cy="16" r="1.6" fill="currentColor" />
      <path d="M30 58h20M40 50v8" strokeLinecap="round" />
      <path d="M28 32h24M28 40h14" strokeLinecap="round" strokeDasharray="3 4" />
    </svg>
  );
}

export function Empty({ title, description, actions, art = 'courses', error = false }: EmptyProps) {
  return (
    <div className={'empty' + (error ? ' empty--error' : '')}>
      <div className="empty__art">
        <Art art={art} />
      </div>
      <div className="empty__title">{title}</div>
      {description && <div className="empty__desc">{description}</div>}
      {actions && <div className="empty__actions">{actions}</div>}
    </div>
  );
}
