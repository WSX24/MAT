/**
 * 全局状态：档案（多档案、本地 active_id）、健康检查、离线标记。
 *
 * 旧前端是「演示账号」，这里换成真实档案：课堂（开课）必须有 profile_id，
 * 所以档案加载失败 = 不能上课，页面会明确说清楚而不是静默降级。
 */
import { computed, reactive } from 'vue'
import { api } from '@/api/client'
import type { Health, Profile } from '@/api/types'
import { errorText } from '@/api/format'

export type HealthState = 'ok' | 'down' | 'unknown'
export type LoadStatus = 'loading' | 'ready' | 'error'

export const appState = reactive({
  profiles: [] as Profile[],
  activeId: '',
  profilesStatus: 'loading' as LoadStatus,
  profilesError: '' as string,
  health: null as Health | null,
  healthState: 'unknown' as HealthState,
})

export const activeProfile = computed<Profile | null>(() => {
  return appState.profiles.find((item) => item.id === appState.activeId) ?? appState.profiles[0] ?? null
})

export const backendOffline = computed(() => appState.profilesStatus === 'error' && appState.healthState === 'down')

let healthTimer = 0
let healthStarted = false

export async function loadProfiles(): Promise<void> {
  appState.profilesStatus = appState.profiles.length ? appState.profilesStatus : 'loading'
  try {
    const data = await api.getProfiles()
    appState.profiles = data.items ?? []
    appState.activeId = data.active_id || appState.profiles[0]?.id || ''
    appState.profilesStatus = 'ready'
    appState.profilesError = ''
  } catch (error) {
    appState.profilesStatus = 'error'
    appState.profilesError = errorText(error)
  }
}

export async function refreshHealth(): Promise<void> {
  try {
    const data = await api.health()
    appState.health = data
    appState.healthState = data.ok ? 'ok' : 'down'
  } catch {
    appState.health = null
    appState.healthState = 'down'
  }
}

export function startAppState(): void {
  void loadProfiles()
  void refreshHealth()
  if (!healthStarted) {
    healthStarted = true
    healthTimer = window.setInterval(() => void refreshHealth(), 30_000)
  }
}

export function stopAppState(): void {
  if (healthTimer) window.clearInterval(healthTimer)
  healthTimer = 0
  healthStarted = false
}

export async function switchProfile(id: string): Promise<void> {
  if (appState.activeId === id) return
  await api.activateProfile(id)
  await loadProfiles()
}

export async function createProfile(name: string): Promise<void> {
  const created = await api.createProfile(name)
  await api.activateProfile(created.id)
  await loadProfiles()
}

export async function removeProfile(id: string): Promise<void> {
  await api.deleteProfile(id)
  await loadProfiles()
}
