import { ApiError } from './client'

/** 秒 → "12 分 30 秒"，超过一小时用 "1 小时 05 分"。 */
export function formatDuration(totalSeconds: number | null | undefined): string {
  const seconds = Math.max(0, Math.round(totalSeconds || 0))
  const hours = Math.floor(seconds / 3600)
  const minutes = Math.floor((seconds % 3600) / 60)
  const rest = seconds % 60
  if (hours > 0) return `${hours} 小时 ${String(minutes).padStart(2, '0')} 分`
  if (minutes > 0) return `${minutes} 分 ${String(rest).padStart(2, '0')} 秒`
  return `${rest} 秒`
}

/** 秒 → "12:30"（课堂里的计时器）。 */
export function formatClock(totalSeconds: number | null | undefined): string {
  const seconds = Math.max(0, Math.round(totalSeconds || 0))
  const minutes = Math.floor(seconds / 60)
  const rest = seconds % 60
  return String(minutes).padStart(2, '0') + ':' + String(rest).padStart(2, '0')
}

export function formatEstimate(seconds: number | null | undefined): string {
  const value = Math.max(0, Math.round(seconds || 0))
  if (value < 60) return `${value} 秒`
  return `约 ${Math.max(1, Math.round(value / 60))} 分钟`
}

export function formatDate(iso: string | null | undefined): string {
  if (!iso) return '—'
  const date = new Date(iso)
  if (Number.isNaN(date.getTime())) return iso
  return `${date.getFullYear()}-${String(date.getMonth() + 1).padStart(2, '0')}-${String(date.getDate()).padStart(2, '0')}`
}

export function errorText(error: unknown): string {
  if (error instanceof ApiError) return error.message
  if (error instanceof Error) return error.message
  return '出了点问题，请稍后重试'
}

const STATUS_LABELS: Record<string, { label: string; tone: 'neutral' | 'brand' | 'ok' | 'warn' }> = {
  not_started: { label: '未开始', tone: 'neutral' },
  in_progress: { label: '进行中', tone: 'brand' },
  paused: { label: '已暂停', tone: 'warn' },
  completed: { label: '已完成', tone: 'ok' },
}

export function statusLabel(status: string | null | undefined): { label: string; tone: 'neutral' | 'brand' | 'ok' | 'warn' } {
  return STATUS_LABELS[status || 'not_started'] ?? { label: status || '未开始', tone: 'neutral' }
}

export function sourceLabel(source: string | null | undefined): string {
  if (source === 'import') return '导入课件'
  if (source === 'builder') return 'AI 生成课程'
  return '出片课程'
}
