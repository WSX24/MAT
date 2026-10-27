import type { ReactNode } from 'react';

export type BadgeTone = 'neutral' | 'brand' | 'ok' | 'warn' | 'danger' | 'outline';

export function Badge({ tone = 'neutral', children }: { tone?: BadgeTone; children: ReactNode }) {
  return <span className={'badge' + (tone === 'neutral' ? '' : ' badge--' + tone)}>{children}</span>;
}
