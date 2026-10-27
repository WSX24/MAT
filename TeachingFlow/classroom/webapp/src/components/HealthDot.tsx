import { useApp } from '../state/AppState';
import { useToast } from './Toast';

export function HealthDot() {
  const { health, healthState, refreshHealth } = useApp();
  const toast = useToast();

  const label = healthState === 'ok' ? '服务正常' : healthState === 'down' ? '服务不可用' : '检查中';
  const dotClass = healthState === 'ok' ? 'dot dot--ok' : healthState === 'down' ? 'dot dot--error' : 'dot';

  const handleClick = async () => {
    await refreshHealth();
    if (healthState === 'ok' && health) {
      toast.info(`服务正常 · v${health.version} · 课程 ${health.courses} 门 · ${health.engine.provider}/${health.engine.model} · TTS ${health.engine.tts}`);
    } else {
      toast.error('连不上后端服务：检查 app.py 是否在跑，或 /api 代理是否指向 127.0.0.1:8000');
    }
  };

  return (
    <button
      type="button"
      className="health"
      onClick={() => void handleClick()}
      title={health ? `v${health.version} · ${health.engine.provider}/${health.engine.model} · TTS ${health.engine.tts}` : '点击立即重查 /api/health'}
      aria-label={`后端健康状态：${label}，点击重新检查`}
    >
      <span className={dotClass} aria-hidden="true" />
      <span>{label}</span>
    </button>
  );
}
