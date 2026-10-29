<script setup lang="ts">
import { computed, onBeforeUnmount, onMounted, ref, watch } from 'vue'
import { useRouter } from 'vue-router'
import ClassroomFab from './ClassroomFab.vue'
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
 *
 * 全屏演示（沿用原型 #fsroot / #fsfab 的两个决定）：
 *   · **真全屏用 Fullscreen API**，不是自己画一层遮罩 —— 只有真全屏才能把地址栏/任务栏
 *     一起吃掉，投屏时才像在放 PPT。被浏览器策略挡住（iframe / 站点策略）时退化成
 *     "铺满页面"的演示态，功能一个不少（原型里有一模一样的退化分支与说明）。
 *   · **全屏里必须看得见的东西要跟着走**：字幕叠在课件底部、控制与提问收进右下角浮窗。
 *     所以全屏宿主是整块课堂，而不是只有课件那一个盒子。
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

/* ---------------------------------------------------------------- 全屏演示 */
/** 全屏宿主：requestFullscreen(它) 之后只有它和它的子树可见。 */
const fsRoot = ref<HTMLElement | null>(null)
/** 演示态：真全屏 或 浏览器不让全屏时的"铺满"退化。 */
const presenting = ref(false)
/** 退化标记：浏览器拒绝了 requestFullscreen 时置位（会让父级铺满屏幕）。 */
const fsFallback = ref(false)
/** 全屏里的叠加字幕开关（关掉只影响全屏那份，侧栏字幕不受影响）。 */
const captionsOn = ref(true)
/** 字幕外观：字号 / 位置 / 颜色。存 localStorage，换一节课仍是上次的设置。 */
const subSize = ref(22)
const subPos = ref<'bottom' | 'top'>('bottom')
const subColor = ref('#111827')
/** 右下角浮窗的展开面板（提问 / 对话 / 设置）。 */
const fabOpen = ref(false)

const CAPTION_PREFS_KEY = 'zhishu:classroom:caption'

function loadCaptionPrefs() {
  try {
    const raw = window.localStorage.getItem(CAPTION_PREFS_KEY)
    if (!raw) return
    const data = JSON.parse(raw) as Partial<{
      on: boolean; size: number; pos: 'bottom' | 'top'; color: string
    }>
    if (typeof data.on === 'boolean') captionsOn.value = data.on
    if (typeof data.size === 'number' && data.size >= 14 && data.size <= 40) subSize.value = data.size
    if (data.pos === 'top' || data.pos === 'bottom') subPos.value = data.pos
    if (typeof data.color === 'string' && /^#[0-9a-fA-F]{6}$/.test(data.color)) subColor.value = data.color
  } catch {
    // 存坏了就当没存：字幕回到默认外观，不该因为一条偏好读不出来而不显示
  }
}

function saveCaptionPrefs() {
  try {
    window.localStorage.setItem(CAPTION_PREFS_KEY, JSON.stringify({
      on: captionsOn.value,
      size: subSize.value,
      pos: subPos.value,
      color: subColor.value,
    }))
  } catch {
    // 隐私模式下 localStorage 会抛，偏好只在本次有效
  }
}

const captionStyle = computed(() => ({
  color: subColor.value,
  fontSize: `${subSize.value}px`,
}))
/**
 * 叠加字幕显示的那一句。
 *
 * 为什么不直接用 ``state.caption``：那是"正在播的这一句"，一播完就清空 —— 全屏时
 * 字幕会随着每句话消失又出现，闪得人没法读。这里保留**最近一句讲过的**，新的一句
 * 开始时自然顶上去；停顿（例如老师等你回应）时屏幕上还留着刚才那句，符合字幕的直觉。
 */
const overlayCaption = ref('')

const fsSupported = computed(() => typeof document !== 'undefined' && !!document.fullscreenEnabled)

function syncPresentation() {
  const real = !!fsRoot.value && document.fullscreenElement === fsRoot.value
  presenting.value = real || fsFallback.value
  if (!presenting.value) fabOpen.value = false
}

async function enterPresentation() {
  const root = fsRoot.value
  if (!root) return
  try {
    await root.requestFullscreen({ navigationUI: 'hide' } as FullscreenOptions)
    fsFallback.value = false
  } catch {
    // 退化但功能不丢：仍然铺满屏幕，字幕、翻页、浮窗照旧。
    fsFallback.value = true
    toast.warn('浏览器不允许进入全屏，已改为铺满页面演示（按 F 或点「退出全屏」恢复）')
  }
  syncPresentation()
}

async function exitPresentation() {
  fsFallback.value = false
  if (document.fullscreenElement) {
    try {
      await document.exitFullscreen()
    } catch {
      // 退出失败就按当前状态同步一次，不留一个假的全屏标记
    }
  }
  syncPresentation()
}

function togglePresentation() {
  if (presenting.value) void exitPresentation()
  else void enterPresentation()
}

function toggleCaptions() {
  captionsOn.value = !captionsOn.value
  saveCaptionPrefs()
}

function setSubSize(value: number) {
  subSize.value = value
  saveCaptionPrefs()
}

function setSubPos(value: 'bottom' | 'top') {
  subPos.value = value
  saveCaptionPrefs()
}

function setSubColor(value: string) {
  subColor.value = value
  saveCaptionPrefs()
}

function onVolume(value: number) {
  classroom.setVolume(value)
}

const classroom = useClassroom({
  sessionId: props.session.session_id,
  startSlide: props.startSlide,
  onRecap: (recap) => goSummary(recap),
})
const state = classroom.state

/* 叠加字幕的两条来源（定义在 state 之后：watch 会在创建时立刻读一次源） */
watch(
  () => state.caption,
  (text) => {
    if (text) overlayCaption.value = text
  },
)

watch(
  () => state.transcript.length,
  () => {
    for (let i = state.transcript.length - 1; i >= 0; i -= 1) {
      const entry = state.transcript[i]
      if (entry.who === 'teacher') {
        overlayCaption.value = entry.text
        return
      }
    }
  },
)

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
  const typing = !!target && (target.tagName === 'INPUT' || target.tagName === 'TEXTAREA' || target.isContentEditable)
  if (event.key === 'Escape' && presenting.value && fabOpen.value) {
    // Esc 先收浮窗，再轮到退全屏（真全屏的 Esc 由浏览器自己处理）
    event.preventDefault()
    fabOpen.value = false
    return
  }
  if (typing) return
  if (event.key === 'f' || event.key === 'F') {
    event.preventDefault()
    togglePresentation()
    return
  }
  if (event.key === 'Escape' && fsFallback.value) {
    // 退化态没有浏览器级的 Esc，只能自己接
    event.preventDefault()
    void exitPresentation()
    return
  }
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
  loadCaptionPrefs()
  const startedAt = Date.now()
  timer = window.setInterval(() => {
    elapsed.value = Math.round((Date.now() - startedAt) / 1000)
  }, 1000)
  window.addEventListener('keydown', onKeyDown)
  // 用户按 Esc / F11 退出时，页面上的"正在演示"状态必须跟着变 —— 否则浮窗留在屏幕上，
  // 而它依赖的 fixed 布局已经回到普通页面，看起来就是两套控件叠在一起。
  document.addEventListener('fullscreenchange', syncPresentation)
})

onBeforeUnmount(() => {
  if (timer) window.clearInterval(timer)
  window.removeEventListener('keydown', onKeyDown)
  document.removeEventListener('fullscreenchange', syncPresentation)
  // 离开课堂时如果还在全屏，把浏览器放回去（否则下一个页面仍在全屏里，很吓人）
  if (document.fullscreenElement) void document.exitFullscreen().catch(() => undefined)
})
</script>

<template>
  <div
    ref="fsRoot"
    class="classroom-route"
    :class="{
      'classroom-route--presenting': presenting,
      'classroom-route--fallback': fsFallback,
      'classroom-route--nocap': !captionsOn,
    }"
  >
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
        <button
          class="icon-btn-sm"
          type="button"
          :aria-label="presenting ? '退出全屏（F）' : '全屏演示（F）'"
          :title="presenting ? '退出全屏（F / Esc）' : '全屏演示（F）'"
          @click="togglePresentation()"
        >
          <svg v-if="!presenting" viewBox="0 0 24 24" aria-hidden="true">
            <path d="M4 9V5.5A1.5 1.5 0 0 1 5.5 4H9M15 4h3.5A1.5 1.5 0 0 1 20 5.5V9M20 15v3.5a1.5 1.5 0 0 1-1.5 1.5H15M4 15v3.5A1.5 1.5 0 0 0 5.5 20H9" />
          </svg>
          <svg v-else viewBox="0 0 24 24" aria-hidden="true">
            <path d="M9 4H5.5A1.5 1.5 0 0 0 4 5.5V9M15 4h3.5A1.5 1.5 0 0 1 20 5.5V9M20 15v3.5a1.5 1.5 0 0 1-1.5 1.5H15M4 15v3.5A1.5 1.5 0 0 0 5.5 20H9" />
          </svg>
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

        <!-- 全屏叠加字幕：和侧栏那份同源（都来自课堂字幕），位置/字号/颜色可在浮窗「设置」里调 -->
        <p
          v-if="presenting && captionsOn && overlayCaption"
          class="fs-caption"
          :class="`fs-caption--${subPos}`"
          :style="captionStyle"
          aria-live="polite"
        >
          {{ overlayCaption }}
        </p>

        <ControlBar
          v-show="!presenting"
          :paused="paused"
          :rate="state.rate"
          :slide-index="state.slideIndex"
          :slide-count="totalSlides"
          :finishing="finishing"
          :disabled="state.status !== 'open'"
          :fullscreen="presenting"
          :fullscreen-supported="fsSupported"
          @toggle-pause="togglePause"
          @prev="classroom.control('prev')"
          @next="classroom.control('next')"
          @rate="onRate"
          @finish="finish"
          @toggle-fullscreen="togglePresentation()"
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

    <!-- 全屏时的右下角浮窗：翻页 / 暂停 / 字幕 / 提问 / 设置 / 退出全屏 -->
    <ClassroomFab
      v-if="presenting"
      v-model:open="fabOpen"
      :paused="paused"
      :rate="state.rate"
      :volume="state.volume"
      :autoplay-blocked="state.autoplayBlocked"
      :slide-index="state.slideIndex"
      :slide-count="totalSlides"
      :captions-on="captionsOn"
      :sub-size="subSize"
      :sub-pos="subPos"
      :sub-color="subColor"
      :entries="state.transcript"
      :connected="state.status === 'open'"
      @prev="classroom.control('prev')"
      @next="classroom.control('next')"
      @toggle-pause="togglePause"
      @toggle-captions="toggleCaptions()"
      @sub-size="setSubSize"
      @sub-pos="setSubPos"
      @sub-color="setSubColor"
      @rate="onRate"
      @volume="onVolume"
      @retry-audio="classroom.retryAudio()"
      @ask="classroom.ask"
      @exit="exitPresentation()"
    />

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
