<script setup lang="ts">
import { computed, onBeforeUnmount, onMounted, ref } from 'vue'
import { RouterLink, useRoute } from 'vue-router'
import ClassroomSession from '@/features/classroom/ClassroomSession.vue'
import { ApiError, api } from '@/api/client'
import { errorText } from '@/api/format'
import { findUnit } from '@/api/view'
import type { CourseDetail, SessionCreated, UnitDetail, UnitPptJob } from '@/api/types'
import { activeProfile, appState, loadProfiles } from '@/state/app'
import { toast } from '@/state/toast'

/**
 * 小节学习 = 真实课堂（课件重绘 + 字幕 + 提问 + 翻页/暂停/语速）。
 *
 * 渐进式出片之后多了一种状态：**这一课还没有课件**。它不是错误，而是流程的一部分
 * （用户只生成了前面几课）。所以这里不报"课件缺失"，而是给一个「生成这节课的 PPT」
 * 的入口，生成完自动进来上课 —— 把"等"变成一次明确的动作。
 */
const POLL_MS = 1500

const route = useRoute()

const course = ref<CourseDetail | null>(null)
const unit = ref<UnitDetail | null>(null)
const session = ref<SessionCreated | null>(null)
const booting = ref(true)
const bootError = ref('')

/** 这一课还没有课件：停在这里给用户一个生成入口，而不是抛错。 */
const needPpt = ref(false)
const pptUnitId = ref('')
const pptJob = ref<UnitPptJob | null>(null)
const generating = ref(false)

let pollTimer: number | null = null
let disposed = false

const unitNumber = computed(() => Number(route.params.unitNumber))
const lessonNumber = computed(() => Number(route.params.lessonNumber))
const courseId = computed(() => String(route.params.id ?? ''))

const startSlide = computed(() => {
  const raw = route.query.slide
  const value = Array.isArray(raw) ? raw[0] : raw
  if (value == null || value === '') return 0
  const parsed = Number.parseInt(String(value), 10)
  return Number.isFinite(parsed) ? Math.max(0, parsed) : 0
})

const pptPercent = computed(() => {
  const current = pptJob.value
  if (!current) return 0
  if (current.status === 'succeeded') return 100
  return Math.max(0, Math.min(100, Math.round(current.percent || 0)))
})

const pptRunning = computed(() => {
  const status = pptJob.value?.status ?? ''
  return generating.value || status === 'queued' || status === 'running'
})

const pptStage = computed(() => {
  const current = pptJob.value
  if (!current) return generating.value ? '正在启动…' : ''
  if (current.status === 'queued') return '排队中'
  return current.stage_label || '正在生成'
})

async function boot() {
  booting.value = true
  bootError.value = ''
  session.value = null
  unit.value = null
  course.value = null
  needPpt.value = false
  pptJob.value = null

  try {
    const detail = await api.getCourse(courseId.value)
    course.value = detail
    const found = findUnit(detail, unitNumber.value, lessonNumber.value)
    if (!found) throw new Error('这个单元不在课程里：课程可能被重新扫描过，回课程大纲重新选一节')

    // 先问"这一课有没有课件"：没有就不去准备（准备必然失败，还会报一句难懂的话）
    const pptState = await api.getUnitPpt(found.unit.id).catch(() => null)
    if (pptState && !pptState.has_ppt) {
      pptUnitId.value = found.unit.id
      pptJob.value = pptState.job
      needPpt.value = true
      if (pptState.job && (pptState.job.status === 'running' || pptState.job.status === 'queued')) {
        schedulePoll()
      }
      return
    }

    let detailUnit = await api.getUnit(found.unit.id)
    if (!detailUnit.ready) {
      const prepared = await api.prepareUnit(found.unit.id)
      if (!prepared.ok && !prepared.deck_ready) {
        throw new Error(prepared.warnings?.[0] || '这个单元的课件还没备好（可能缺源文件）')
      }
      detailUnit = await api.getUnit(found.unit.id)
    }

    if (!activeProfile.value) await loadProfiles()
    if (!activeProfile.value) {
      throw new Error('还没有学习档案：先在左下角新建一个档案，再回来上课')
    }

    const created = await api.createSession({
      unit_id: found.unit.id,
      profile_id: activeProfile.value.id,
      start_slide: startSlide.value || undefined,
    })
    unit.value = detailUnit
    session.value = created
  } catch (error) {
    bootError.value = errorText(error)
  } finally {
    booting.value = false
  }
}

function stopPolling() {
  if (pollTimer !== null) {
    window.clearTimeout(pollTimer)
    pollTimer = null
  }
}

function schedulePoll() {
  stopPolling()
  if (disposed) return
  pollTimer = window.setTimeout(() => { void poll() }, POLL_MS)
}

async function poll() {
  pollTimer = null
  if (disposed || !pptUnitId.value) return
  try {
    const state = await api.getUnitPpt(pptUnitId.value)
    pptJob.value = state.job
    if (state.has_ppt) {
      needPpt.value = false
      toast.ok('课件生成好了，正在进入课堂')
      await boot()
      return
    }
    const status = state.job?.status ?? ''
    if (status === 'failed') {
      toast.error(state.job?.error || '课件生成失败')
      return
    }
    if (status === 'cancelled' || status === 'interrupted') return
    schedulePoll()
  } catch {
    schedulePoll()
  }
}

async function generatePpt() {
  if (!pptUnitId.value || pptRunning.value) return
  generating.value = true
  try {
    pptJob.value = await api.generateUnitPpt(pptUnitId.value)
    toast.ok('开始生成这节课的课件')
    schedulePoll()
  } catch (error) {
    if (error instanceof ApiError && error.status === 409) {
      toast.warn(error.message)
      schedulePoll()
    } else {
      toast.error(errorText(error))
    }
  } finally {
    generating.value = false
  }
}

const backRoute = computed(() => ({ name: 'app-course-detail', params: { id: courseId.value } }))

onMounted(boot)
onBeforeUnmount(() => { disposed = true; stopPolling() })
</script>

<template>
  <div class="lesson-page">
    <RouterLink v-if="!(session && unit)" class="lesson-back" :to="backRoute">
      <svg viewBox="0 0 24 24" aria-hidden="true"><path d="m15 18-6-6 6-6M9 12h11" /></svg>
      返回课程大纲
    </RouterLink>

    <div v-if="booting" class="lesson-loading" role="status">
      <span class="loading-bar" aria-hidden="true" />
      <span>正在打开课堂…</span>
    </div>

    <section v-else-if="needPpt" class="lesson-panel lesson-panel--ppt">
      <span class="lesson-panel-icon" aria-hidden="true">▷</span>
      <h1>这一课的课件还没生成</h1>
      <p>
        课件是按课生成的：上到哪一课，生成哪一课，这样不用一次性排完所有课件。
        点下面的按钮，我会用这份大纲里这一课的知识点清单和教学安排出片。
      </p>
      <div v-if="pptRunning" class="ppt-progress" role="progressbar" :aria-valuenow="pptPercent" aria-valuemin="0" aria-valuemax="100">
        <span class="ppt-progress-bar"><i :style="{ width: pptPercent + '%' }" /></span>
        <span class="ppt-progress-text">{{ pptStage }} · {{ pptPercent }}%</span>
      </div>
      <p v-else-if="pptJob?.status === 'failed'" class="ppt-error">{{ pptJob.error }}</p>
      <div class="lesson-panel-actions">
        <button class="btn btn-primary" type="button" :disabled="pptRunning" @click="generatePpt()">
          {{ pptRunning ? '正在生成…' : (pptJob?.status === 'failed' ? '重试生成课件' : '生成这节课的 PPT') }}
        </button>
        <RouterLink class="btn" :to="backRoute">返回课程大纲</RouterLink>
      </div>
      <p class="lesson-panel-note">生成一节课大约 2~5 分钟，可以先去别的页面，回来再看进度。</p>
    </section>

    <section v-else-if="bootError" class="lesson-panel lesson-panel--error">
      <span class="lesson-panel-icon" aria-hidden="true">!</span>
      <h1>课堂没能打开</h1>
      <p>{{ bootError }}</p>
      <div class="lesson-panel-actions">
        <button class="btn btn-primary" type="button" @click="boot">重试</button>
        <RouterLink class="btn" :to="backRoute">返回课程大纲</RouterLink>
        <RouterLink v-if="appState.profilesStatus === 'error'" class="btn" :to="{ name: 'app-courses' }">回课程库</RouterLink>
      </div>
    </section>

    <ClassroomSession
      v-else-if="session && unit"
      :key="`${session.session_id}:${startSlide}`"
      :unit="unit"
      :session="session"
      :start-slide="startSlide || session.position.slide"
      :course-id="courseId"
      :unit-number="unitNumber"
      :lesson-number="lessonNumber"
    />

    <section v-else class="lesson-panel">
      <span class="lesson-panel-icon" aria-hidden="true">?</span>
      <h1>没有找到这个小节</h1>
      <p>课程或小节地址可能已失效，请回到课程列表重新选择。</p>
      <RouterLink class="btn btn-primary" :to="{ name: 'app-courses' }">返回我的课堂</RouterLink>
    </section>
  </div>
</template>

<style scoped>
.lesson-page { max-width: 1280px; min-height: 60vh; margin-inline: auto; }
.lesson-back { display: inline-flex; align-items: center; gap: 7px; margin-bottom: 16px; color: var(--muted); font-size: 12px; transition: color 140ms ease; }
.lesson-back:hover { color: var(--accent); }
.lesson-back svg { width: 16px; height: 16px; fill: none; stroke: currentColor; stroke-linecap: round; stroke-linejoin: round; stroke-width: 1.7; }
.lesson-loading { display: flex; align-items: center; gap: 10px; padding: 44px 4px; color: var(--muted); font-size: 12px; }
.loading-bar { width: 42px; height: 2px; border-radius: 2px; background: linear-gradient(90deg, var(--border), var(--accent), var(--border)); background-size: 200% 100%; animation: bar 1.2s linear infinite; }
@keyframes bar { to { background-position: -200% 0; } }

.lesson-panel { display: flex; min-height: 50vh; flex-direction: column; align-items: center; justify-content: center; text-align: center; }
.lesson-panel-icon { display: grid; width: 46px; height: 46px; place-items: center; border-radius: 15px; background: color-mix(in oklch, var(--accent) 9%, var(--surface)); color: var(--accent); font-size: 20px; }
.lesson-panel h1 { margin-top: 15px; font-size: 21px; }
.lesson-panel p { max-width: 54ch; margin-top: 8px; color: var(--muted); font-size: 12px; line-height: 1.75; }
.lesson-panel-actions { display: flex; flex-wrap: wrap; justify-content: center; gap: 9px; margin-top: 17px; }
.lesson-panel-note { color: var(--meta) !important; font-size: 10.5px !important; }

.ppt-progress { width: min(100%, 380px); margin-top: 16px; }
.ppt-progress-bar { display: block; height: 5px; overflow: hidden; border-radius: 3px; background: var(--border-soft); }
.ppt-progress-bar i { display: block; height: 100%; border-radius: inherit; background: var(--accent); transition: width 280ms ease; }
.ppt-progress-text { display: block; margin-top: 7px; color: var(--muted); font-family: var(--font-mono); font-size: 10px; }
.ppt-error { max-width: 54ch; margin-top: 12px; color: var(--danger) !important; font-size: 11px !important; }
</style>
