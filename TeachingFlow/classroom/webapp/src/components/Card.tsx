import type { HTMLAttributes } from 'react';

export interface CardProps extends HTMLAttributes<HTMLDivElement> {
  interactive?: boolean;
  flush?: boolean;
}

export function Card({ interactive = false, flush = false, className = '', children, ...rest }: CardProps) {
  const classes = ['card', interactive ? 'card--interactive' : '', flush ? 'card--flush' : '', className]
    .filter(Boolean)
    .join(' ');
  return (
    <div className={classes} {...rest}>
      {children}
    </div>
  );
}
