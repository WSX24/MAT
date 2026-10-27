export interface ProgressBarProps {
  value: number;
  size?: 'sm' | 'md';
  label?: string;
}

export function ProgressBar({ value, size = 'md', label }: ProgressBarProps) {
  const clamped = Math.max(0, Math.min(100, Math.round(value || 0)));
  return (
    <div
      className={['progress', size === 'sm' ? 'progress--sm' : '', clamped >= 100 ? 'progress--complete' : '']
        .filter(Boolean)
        .join(' ')}
      role="progressbar"
      aria-valuenow={clamped}
      aria-valuemin={0}
      aria-valuemax={100}
      aria-label={label || '学习进度'}
      style={{ ['--p' as string]: clamped + '%' }}
    >
      <div className="progress__bar" />
    </div>
  );
}

export interface ProgressRingProps {
  value: number;
  size?: number;
  stroke?: number;
  label?: string;
}

export function ProgressRing({ value, size = 64, stroke = 6, label }: ProgressRingProps) {
  const clamped = Math.max(0, Math.min(100, Math.round(value || 0)));
  const radius = (size - stroke) / 2;
  const circumference = 2 * Math.PI * radius;
  const dash = (clamped / 100) * circumference;
  return (
    <div
      className="progress-ring"
      style={{ ['--size' as string]: size + 'px' }}
      role="img"
      aria-label={`${label || '学习进度'} ${clamped}%`}
    >
      <svg width={size} height={size} viewBox={`0 0 ${size} ${size}`} aria-hidden="true">
        <circle cx={size / 2} cy={size / 2} r={radius} fill="none" stroke="var(--surface-3)" strokeWidth={stroke} />
        <circle
          cx={size / 2}
          cy={size / 2}
          r={radius}
          fill="none"
          stroke={clamped >= 100 ? 'var(--ok)' : 'var(--brand)'}
          strokeWidth={stroke}
          strokeLinecap="round"
          strokeDasharray={`${dash} ${circumference - dash}`}
        />
      </svg>
      <span className="progress-ring__value tabular">{clamped}%</span>
    </div>
  );
}
