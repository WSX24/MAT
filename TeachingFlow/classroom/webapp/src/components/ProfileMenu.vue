<script setup lang="ts">
import { computed, onBeforeUnmount, onMounted, ref } from 'vue'
import { toast } from '@/state/toast'
import { activeProfile, appState, createProfile, removeProfile, switchProfile } from '@/state/app'
import { errorText } from '@/api/format'

/**
 * 侧栏底的档案切换器。
 *
 * 旧前端这里是「演示账号 / 本地前端预览」的静态占位；本仓库有真实档案表，
 * 所以换成：切换档案、新建档案、删除当前档案，外加一行服务状态（30s 轮询健康检查）。
 */
const open = ref(false)
const busy = ref(false)
const newName = ref('')
const root = ref<HTMLElement | null>(null)

const initial = computed(() => (activeProfile.value?.name || '?').slice(0, 1))
const statusLine = computed(() => {
  // 上半行已经写了「未连接后端」，这里补的是"能做什么"，否则两行字一样
  if (appState.profilesStatus === 'error') return '档案暂不可用，点开查看'
  if (appState.profilesStatus === 'loading') return '正在加载档案'
  return `${appState.profiles.length} 个学习档案`
})
const healthLabel = computed(() => {
  if (appState.healthState === 'ok') return '服务正常'
  if (appState.healthState === 'down') return '服务不可用'
  return '检查中'
})

function onDocumentClick(event: MouseEvent) {
  if (!open.value) return
  const target = event.target as Node | null
  if (root.value && target && !root.value.contains(target)) open.value = false
}

async function choose(id: string) {
  if (busy.value || id === appState.activeId) return
  busy.value = true
  try {
    await switchProfile(id)
    toast.ok(`已切换到「${appState.profiles.find((item) => item.id === id)?.name ?? ''}」`)
  } catch (error) {
    toast.error(errorText(error))
  } finally {
    busy.value = false
  }
}

async function submitCreate() {
  const name = newName.value.trim()
  if (!name || busy.value) return
  busy.value = true
  try {
    await createProfile(name)
    newName.value = ''
    toast.ok(`档案「${name}」已创建并切换`)
  } catch (error) {
    toast.error(errorText(error))
  } finally {
    busy.value = false
  }
}

async function removeCurrent() {
  const current = activeProfile.value
  if (!current || busy.value) return
  if (!window.confirm(`删除档案「${current.name}」？该档案的学习进度会一起删除。`)) return
  busy.value = true
  try {
    await removeProfile(current.id)
    toast.ok('档案已删除')
  } catch (error) {
    toast.error(errorText(error))
  } finally {
    busy.value = false
  }
}

onMounted(() => document.addEventListener('click', onDocumentClick))
onBeforeUnmount(() => document.removeEventListener('click', onDocumentClick))
</script>

<template>
  <div ref="root" class="side-user-wrap">
    <button
      class="side-user"
      type="button"
      :aria-expanded="open"
      aria-haspopup="menu"
      @click="open = !open"
    >
      <span class="avatar avatar--accent" aria-hidden="true">{{ initial }}</span>
      <span class="side-user-copy">
        <span class="profile-name">{{ activeProfile?.name ?? '未连接后端' }}</span>
        <span class="caption">{{ statusLine }}</span>
      </span>
      <svg class="chev" :class="{ 'is-open': open }" viewBox="0 0 24 24" aria-hidden="true">
        <path d="m7 10 5 5 5-5" />
      </svg>
    </button>

    <div v-if="open" class="profile-menu" role="menu" aria-label="学习档案">
      <p class="profile-menu-label">学习档案</p>
      <button
        v-for="profile in appState.profiles"
        :key="profile.id"
        class="profile-menu-item"
        type="button"
        role="menuitemradio"
        :aria-checked="profile.id === appState.activeId"
        :disabled="busy"
        @click="choose(profile.id)"
      >
        <span class="profile-menu-avatar" aria-hidden="true">{{ profile.name.slice(0, 1) }}</span>
        <span class="profile-menu-name">{{ profile.name }}</span>
        <span v-if="profile.id === appState.activeId" class="profile-menu-now">当前</span>
      </button>
      <p v-if="!appState.profiles.length" class="profile-menu-empty">
        {{ appState.profilesStatus === 'error' ? '后端未连接，拿不到档案' : '还没有档案' }}
      </p>

      <form class="profile-menu-form" @submit.prevent="submitCreate">
        <input v-model="newName" type="text" maxlength="40" placeholder="新档案名，例如「小明」" aria-label="新档案名" :disabled="busy">
        <button class="profile-menu-create" type="submit" :disabled="busy || !newName.trim()">新建</button>
      </form>

      <button
        v-if="appState.profiles.length > 1"
        class="profile-menu-delete"
        type="button"
        :disabled="busy"
        @click="removeCurrent"
      >删除当前档案</button>

      <p class="profile-menu-health">
        <i class="health-dot" :class="`health-dot--${appState.healthState}`" aria-hidden="true" />
        {{ healthLabel }}<template v-if="appState.health?.version"> · v{{ appState.health.version }}</template>
      </p>
    </div>
  </div>
</template>

<style scoped>
.side-user-wrap { position: relative; }

.side-user {
  display: flex;
  width: 100%;
  align-items: center;
  gap: 10px;
  padding: 8px;
  border: 1px solid transparent;
  border-radius: 11px;
  background: transparent;
  color: inherit;
  cursor: pointer;
  font: inherit;
  text-align: left;
  transition: background 140ms ease, border-color 140ms ease;
}
.side-user:hover { border-color: var(--border); background: var(--surface); }
.profile-name { overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
.caption { color: var(--meta); font-size: 10px; }
.chev { width: 15px; height: 15px; flex: none; fill: none; stroke: var(--meta); stroke-linecap: round; stroke-linejoin: round; stroke-width: 1.7; transition: transform 140ms ease; }
.chev.is-open { transform: rotate(180deg); }

.profile-menu {
  position: absolute;
  z-index: 40;
  bottom: calc(100% + 8px);
  left: 0;
  width: 262px;
  padding: 10px;
  border: 1px solid var(--border);
  border-radius: 13px;
  background: var(--bg);
  box-shadow: 0 18px 38px color-mix(in oklch, var(--fg) 12%, transparent);
}
.profile-menu-label { margin: 2px 4px 7px; color: var(--meta); font-size: 10px; letter-spacing: .04em; }
.profile-menu-item {
  display: flex;
  width: 100%;
  align-items: center;
  gap: 9px;
  padding: 7px 8px;
  border: 0;
  border-radius: 9px;
  background: transparent;
  color: var(--fg);
  cursor: pointer;
  font: inherit;
  font-size: 12px;
  text-align: left;
}
.profile-menu-item:hover { background: var(--surface); }
.profile-menu-item[aria-checked="true"] { background: color-mix(in oklch, var(--accent) 7%, var(--surface)); color: var(--accent); }
.profile-menu-avatar {
  display: grid;
  width: 22px;
  height: 22px;
  flex: none;
  place-items: center;
  border-radius: 7px;
  background: var(--border-soft);
  color: var(--muted);
  font-size: 10px;
  font-weight: 650;
}
.profile-menu-item[aria-checked="true"] .profile-menu-avatar { background: color-mix(in oklch, var(--accent) 14%, var(--surface)); color: var(--accent); }
.profile-menu-name { flex: 1; min-width: 0; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
.profile-menu-now { flex: none; color: var(--accent); font-size: 9px; }
.profile-menu-empty { margin: 4px 8px 8px; color: var(--meta); font-size: 11px; }

.profile-menu-form { display: flex; gap: 6px; margin-top: 8px; padding-top: 9px; border-top: 1px solid var(--border-soft); }
.profile-menu-form input {
  width: 100%;
  min-width: 0;
  height: 31px;
  padding: 0 9px;
  border: 1px solid var(--border);
  border-radius: 8px;
  background: var(--surface);
  color: var(--fg);
  font: inherit;
  font-size: 11px;
}
.profile-menu-form input:focus { outline: 2px solid color-mix(in oklch, var(--accent) 45%, transparent); outline-offset: 1px; }
.profile-menu-create {
  flex: none;
  padding: 0 11px;
  border: 0;
  border-radius: 8px;
  background: var(--accent);
  color: #fff;
  cursor: pointer;
  font: inherit;
  font-size: 11px;
}
.profile-menu-create:disabled { cursor: not-allowed; opacity: .45; }
.profile-menu-delete {
  width: 100%;
  margin-top: 7px;
  padding: 6px 8px;
  border: 0;
  border-radius: 8px;
  background: transparent;
  color: var(--danger);
  cursor: pointer;
  font: inherit;
  font-size: 10px;
  text-align: left;
}
.profile-menu-delete:hover { background: color-mix(in oklch, var(--danger) 7%, transparent); }
.profile-menu-health { display: flex; align-items: center; gap: 6px; margin: 8px 4px 1px; color: var(--meta); font-size: 10px; }
.health-dot { width: 6px; height: 6px; border-radius: 50%; background: var(--meta); }
.health-dot--ok { background: var(--success); }
.health-dot--down { background: var(--danger); }
</style>
