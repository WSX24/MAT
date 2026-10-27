import type { CSSProperties } from 'react';

export function Skeleton({
  height = 14,
  width = '100%',
  radius,
  style,
  className = '',
}: {
  height?: number | string;
  width?: number | string;
  radius?: number | string;
  style?: CSSProperties;
  className?: string;
}) {
  return (
    <div
      className={['skeleton', className].filter(Boolean).join(' ')}
      style={{ height, width, borderRadius: radius, ...style }}
      aria-hidden="true"
    />
  );
}

export function SkeletonCards({ count = 6 }: { count?: number }) {
  return (
    <div className="skeleton-grid" role="status" aria-label="正在加载课程">
      {Array.from({ length: count }).map((_, index) => (
        <div className="skeleton-card" key={index}>
          <Skeleton width={64} height={24} radius={999} />
          <Skeleton height={22} width="82%" />
          <Skeleton height={14} width="55%" />
          <Skeleton height={8} radius={999} />
          <Skeleton height={38} radius={999} />
        </div>
      ))}
    </div>
  );
}

export function SkeletonRows({ count = 4 }: { count?: number }) {
  return (
    <div className="stack" role="status" aria-label="正在加载">
      {Array.from({ length: count }).map((_, index) => (
        <div className="card" key={index} style={{ display: 'flex', flexDirection: 'column', gap: 12 }}>
          <Skeleton height={20} width="40%" />
          <Skeleton height={14} width="92%" />
          <Skeleton height={14} width="70%" />
        </div>
      ))}
    </div>
  );
}
