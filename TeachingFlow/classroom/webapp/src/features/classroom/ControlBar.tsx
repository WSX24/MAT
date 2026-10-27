import { Button } from '../../components/Button';
import { IconNext, IconPause, IconPlay, IconPrev, IconStop } from '../../components/icons';
import type { Rate } from './useClassroom';

export interface ControlBarProps {
  paused: boolean;
  rate: Rate;
  slideIndex: number;
  slideCount: number;
  finishing: boolean;
  disabled: boolean;
  onTogglePause: () => void;
  onPrev: () => void;
  onNext: () => void;
  onRate: (rate: Rate) => void;
  onFinish: () => void;
}

const RATES: Rate[] = [0.8, 1, 1.2];

export function ControlBar({
  paused,
  rate,
  slideIndex,
  slideCount,
  finishing,
  disabled,
  onTogglePause,
  onPrev,
  onNext,
  onRate,
  onFinish,
}: ControlBarProps) {
  return (
    <div className="control-bar" role="toolbar" aria-label="课堂控制">
      <Button
        variant="primary"
        size="sm"
        onClick={onTogglePause}
        disabled={disabled}
        aria-label={paused ? '继续讲解（空格）' : '暂停讲解（空格）'}
        iconLeft={paused ? <IconPlay size={16} /> : <IconPause size={16} />}
      >
        {paused ? '继续' : '暂停'}
      </Button>

      <div className="control-bar__group">
        <Button
          variant="secondary"
          size="sm"
          iconOnly
          aria-label="上一页（←）"
          title="上一页（←）"
          onClick={onPrev}
          disabled={disabled || slideIndex <= 0}
          iconLeft={<IconPrev size={18} />}
        />
        <span className="tabular" aria-live="polite" style={{ minWidth: 64, textAlign: 'center', fontSize: 'var(--text-sm)' }}>
          {slideCount ? `${slideIndex + 1} / ${slideCount}` : '—'}
        </span>
        <Button
          variant="secondary"
          size="sm"
          iconOnly
          aria-label="下一页（→）"
          title="下一页（→）"
          onClick={onNext}
          disabled={disabled || slideCount === 0 || slideIndex >= slideCount - 1}
          iconLeft={<IconNext size={18} />}
        />
      </div>

      <div className="control-bar__group" role="group" aria-label="语速">
        <span className="control-bar__hint">语速</span>
        <div className="segmented">
          {RATES.map((value) => (
            <button
              key={value}
              type="button"
              className="segmented__item"
              aria-pressed={rate === value}
              aria-label={`语速 ${value} 倍`}
              onClick={() => onRate(value)}
            >
              {value}×
            </button>
          ))}
        </div>
      </div>

      <span className="spacer" />
      <span className="control-bar__hint">空格暂停/继续 · ←→ 翻页</span>
      <Button
        variant="danger"
        size="sm"
        loading={finishing}
        onClick={onFinish}
        aria-label="结束课堂并看小结"
        iconLeft={<IconStop size={16} />}
      >
        结束课堂
      </Button>
    </div>
  );
}
