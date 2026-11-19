/** 轻量提示条：不引入 UI 库，用设计令牌画。 */
import { reactive } from 'vue'

export type ToastLevel = 'info' | 'ok' | 'warn' | 'error'

export interface ToastItem {
  id: number
  level: ToastLevel
  message: string
}

export const toastItems = reactive<ToastItem[]>([])

let seq = 0
const timers = new Set<number>()

export function dismissToast(id: number): void {
  const index = toastItems.findIndex((item) => item.id === id)
  if (index >= 0) toastItems.splice(index, 1)
}

export function pushToast(level: ToastLevel, message: string): void {
  seq += 1
  const id = seq
  toastItems.push({ id, level, message })
  while (toastItems.length > 4) toastItems.shift()
  const timer = window.setTimeout(() => {
    timers.delete(timer)
    dismissToast(id)
  }, level === 'error' ? 7000 : 4200)
  timers.add(timer)
}

export const toast = {
  info: (message: string) => pushToast('info', message),
  ok: (message: string) => pushToast('ok', message),
  warn: (message: string) => pushToast('warn', message),
  error: (message: string) => pushToast('error', message),
}
