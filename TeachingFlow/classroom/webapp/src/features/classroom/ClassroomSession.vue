<script setup lang="ts">
import { computed, onBeforeUnmount, onMounted, ref, watch } from 'vue'
import { useRouter } from 'vue-router'
import ControlBar from './ControlBar.vue'
import TranscriptPanel from './TranscriptPanel.vue'
import { useClassroom } from './useClassroom'
import SlideView from '@/features/deck/SlideView.vue'
import { api } from '@/api/client'
import { errorText, formatClock } from '@/api/format'
import type { Recap, SessionCreated, UnitDetail } from '@/api/types'
import { toast } from '@/state/toast'

/**
 * 一次课堂会话（/learn 那一层）。
 *
 * 独立成组件是刻意的：useClassroom 在 setup 里建立 WebSocket，会话换了就必须
 * 重新挂载；由父级用 session_id 做 key，不靠 watch 手搓重连。
 * 交互沿用旧版：空格暂停、←→ 翻页、语速 0.8/1/1.2、结束课堂 → 课后小结。
 */
const props = defineProps<{
  unit: UnitDetail
  session: SessionCreated
  startSlide: number
  courseId: string
  unitNumber: number
  lessonNumber: number
}>()

const router = useRouter()
const drawerOpen = ref(false)
const elapsed = ref(0)
const finishing = ref(false)
let navigated = false
let timer = 0

const classroom = useClassroom({
  sessionId: props.session.session_id,
  startSlide: props.startSlide,
  onRecap: (recap) => goSummary(recap),
})
const state = classroom.state

const totalSlides = computed(() => state.slideCount || props.unit.slides || 0)
const currentSlide = computed(() =>
  totalSlides.value ? Math.min(state.slideIndex + 1, totalSlides.value) : state.slideIndex + 1,
)
const paused = computed(() => state.paused || state.phase === 'paused')
const phaseText = computed(() => {
  if (paused.value) return '已暂停'
  if (state.phase === 'ended') return '已结束'
  if (state.status === 'open') return '讲解中'
  return '连接中'
})
const phaseTone = computed(() => (paused.value ? 'warn' : state.phase === 'ended' ? 'neutral' : 'live'))
const comprehensionText = computed(() =>
  Number.isFinite(state.comprehension) ? `理解度 ${Math.round((state.comprehension ?? 0) * 100)}%` : '',
)

function goSummary(recap: Recap) {
  if (navigated) return
  navigated = true
  void router.replace({
    name: 'app-session-summary',
    params: { id: props.session.session_id },
    // history.state 只接受可序列化的原始值/普通对象，打包成 JSON 字符串最稳。
    state: {
      summary: JSON.stringify({
        recap,
        courseId: props.courseId,
        unitId: props.unit.id,
        unitNumber: props.unitNumber,
        lessonNumber: props.lessonNumber,
        unitTitle: props.unit.title,
      }),
    },
  })
}

function togglePause() {
  classroom.control(paused.value ? 'resume' : 'pause')
}

function onRate(value: 0.8 | 1 | 1.2) {
  classroom.setRate(value)
}

async function finish() {
  if (finishing.value) return
  finishing.value = true
  classroom.control('finish')
  try {
    const result = await api.endSession(props.session.session_id)
    goSummary(result.recap)
  } catch (error) {
    toast.error(errorText(error))
    finishing.value = false
  }
}

function onKeyDown(event: KeyboardEvent) {
  const target = event.target as HTMLElement | null
  if (target && (target.tagName === 'INPUT' || target.tagName === 'TEXTAREA' || target.isContentEditable)) return
  if (event.code === 'Space') {
    event.preventDefault()
    togglePause()
  } else if (event.key === 'ArrowRight') {
    event.preventDefault()
    classroom.control('next')
  } else if (event.key === 'ArrowLeft') {
    event.preventDefault()
    classroom.control('prev')
  }
}

watch(
  () => state.autoplayBlocked,
  (blocked) => {
    if (blocked) toast.warn('浏览器挡住了自动播放：点右上角「播放声音」继续')
  },
)

onMounted(() => {
  const startedAt = Date.now()
  timer = window.setInterval(() => {
    elapsed.value = Math.round((Date.now() - startedAt) / 1000)
  }, 1000)
  window.addEventListener('keydown', onKeyDown)
})

onBeforeUnmount(() => {
  if (timer) window.clearInterval(timer)
  window.removeEventListener('keydown', onKeyDown)
})
</script>

<template>
  <div class="classroom-route">
    <div class="classroom-head">
      <button class="back-btn" type="button" aria-label="返回课程大纲" @click="router.push({ name: 'app-course-detail', params: { id: props.courseId } })">
        <svg viewBox="0 0 24 24" aria-hidden="true"><path d="m15 18-6-6 6-6M9 12h11" /></svg>
      </button>
      <div class="classroom-head-copy">
        <div class="classroom-head-title">{{ props.unit.title }}</div>
        <div class="classroom-head-meta">
          <span>第 {{ currentSlide }} / {{ totalSlides }} 页</span>
          <span>{{ formatClock(elapsed) }}</span>
          <span class="phase-chip" :class="`phase-chip--${phaseTone}`">{{ phaseText }}</span>
          <span v-if="comprehensionText">{{ comprehensionText }}</span>
        </div>
      </div>
      <span class="classroom-head-spacer" />
      <div class="classroom-head-actions">
        <button v-if="state.autoplayBlocked" class="icon-btn-sm" type="button" aria-label="播放声音" title="播放声音" @click="classroom.retryAudio()">
          <svg viewBox="0 0 24 24" aria-hidden="true"><path d="M4 9.5h3.5L12 5.5v13L7.5 14.5H4zM16 8.5a4.5 4.5 0 0 1 0 7" /></svg>
        </button>
        <router-link class="btn" :to="{ name: 'app-unit-outline', params: { unitId: props.unit.id } }">单元大纲</router-link>
        <button class="btn sidebar-toggle" type="button" :aria-expanded="drawerOpen" aria-controls="classroom-sidebar" @click="drawerOpen = !drawerOpen">
          字幕与提问
        </button>
      </div>
    </div>

    <div v-if="state.status !== 'open'" class="conn-banner" role="status">
      <span class="dot" aria-hidden="true" />
      <span>
        {{ state.status === 'connecting' || state.status === 'idle'
          ? '正在连接课堂…'
          : `连接断开，正在自动重连（第 ${Math.max(1, state.retries)} 次）…重连后会从当前进度恢复` }}
      </span>
    </div>

    <div class="classroom-body">
      <div class="stage-wrap">
        <div class="stage">
          <SlideView v-if="state.slide" :slide="state.slide" />
          <div v-else class="stage-empty">
            <span class="dot" aria-hidden="true" />
            <span>正在准备这一页的课件…</span>
          </div>
        </div>

        <ControlBar
          :paused="paused"
          :rate="state.rate"
          :slide-index="state.slideIndex"
          :slide-count="totalSlides"
          :finishing="finishing"
          :disabled="state.status !== 'open'"
          @toggle-pause="togglePause"
          @prev="classroom.control('prev')"
          @next="classroom.control('next')"
          @rate="onRate"
          @finish="finish"
        />
      </div>

      <aside
        id="classroom-sidebar"
        class="classroom-sidebar"
        :class="{ 'classroom-sidebar--open': drawerOpen }"
        aria-label="字幕与提问"
      >
        <TranscriptPanel
          :entries="state.transcript"
          :caption="state.caption"
          :connected="state.status === 'open'"
          @ask="classroom.ask"
        />
      </aside>
    </div>

    <button v-if="drawerOpen" class="sidebar-scrim" type="button" aria-label="收起字幕面板" @click="drawerOpen = false" />
  </div>
</template>

<style scoped>
.sidebar-scrim {
  position: fixed;
  z-index: 55;
  inset: 0;
  border: 0;
  background: color-mix(in oklch, var(--fg) 22%, transparent);
}
@media (min-width: 1081px) {
  .sidebar-scrim { display: none; }
}
</style>
