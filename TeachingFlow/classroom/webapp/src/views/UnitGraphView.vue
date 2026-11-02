<script setup lang="ts">
import { computed, onBeforeUnmount, onMounted, ref, shallowRef } from 'vue'
import { RouterLink, useRoute } from 'vue-router'
import { ApiError, api } from '@/api/client'
import { errorText, formatEstimate } from '@/api/format'
import type { GraphLive, UnitGraph } from '@/api/types'
import { activeProfile } from '@/state/app'
import GraphCanvas from '@/features/graph/GraphCanvas.vue'
import GraphDetail from '@/features/graph/GraphDetail.vue'
import {
  buildModel,
  emptyLive,
  emptyModel,
  normalizeLive,
  resolveCurrentIndex,
  type GraphModel,
  type NodeView,
  type RouteView,
} from '@/features/graph/model'

/**
 * 课堂走向图（GET /api/v1/units/{id}/graph）。只读。
 *
 * 这一页要回答一个问题：这节课有哪几格、从哪岔出、汇回哪里、现在讲到哪。
 * 所以数据分成两份存：
 *   · graph / model —— 首屏那份静态结构（格子、路线、页标尺），只有加载/重试才会换；
 *   · live          —— 课堂指针，每 5 秒刷一次。
 * 分开存是为了让轮询只碰 live：整页重算会让卡片重排、缩放平移被重置，
 * 那正是「一眼看清」最怕的抖动。
 */
const route = useRoute()
const unitId = computed(() => String(route.params.unitId ?? ''))

const POLL_MS = 5000

const graph = shallowRef<UnitGraph | null>(null)
const model = shallowRef<GraphModel>(emptyModel())
const live = ref<GraphLive>(emptyLive())
const loading = ref(true)
const error = ref('')
/** 画布现在还有没有可平移的空间（由画布根据缩放算，提示语跟着它换）。 */
const pannable = ref(true)
const zoom = ref(1)
const selection = ref<{ kind: 'node'; index: number } | { kind: 'route'; key: string } | null>(null)
const canvas = ref<InstanceType<typeof GraphCanvas> | null>(null)

const ok = computed(() => graph.value?.ok !== false)
const hasGraph = computed(() => ok.value && model.value.nodes.length > 0)
const hintText = computed(() => String(graph.value?.hint || '这节课的走向图暂时打不开，稍后重试。'))
const title = computed(() => String(graph.value?.unit?.title || '课堂走向图'))

const outlineLink = computed(() => ({ name: 'app-unit-outline', params: { unitId: unitId.value } }))

/** 现场解析当前格：接口给的是格号，认不出来再按下标解 —— 解析不到就不画指针，绝不猜。 */
const currentIndex = computed(() => resolveCurrentIndex(model.value.nodes, live.value))
const currentPage = computed(() => {
  const index = currentIndex.value
  const node = index == null ? null : model.value.nodes[index]
  if (node && node.page > 0) return node.page
  return live.value.slide && live.value.slide > 0 ? live.value.slide : 0
})

const statusText = computed(() => {
  if (live.value.running) return '课堂进行中'
  if (live.value.source === 'progress') {
    return currentPage.value > 0 ? `上次讲到第 ${currentPage.value} 页` : '上次的进度还停在半路'
  }
  return '课堂没在跑'
})

const currentText = computed(() => {
  if (live.value.source === 'progress' && currentPage.value > 0) return `上次讲到第 ${currentPage.value} 页`
  return '你现在在这里'
})

const stats = computed(() => {
  const totals = model.value.totals
  return [
    `主线 ${totals.nodes} 格`,
    `支线 ${totals.routes} 条`,
    `共 ${totals.slides} 页`,
    `预计时长 ${formatEstimate(totals.est_sec)}`,
  ]
})

const selectedNode = computed<NodeView | null>(() => {
  const current = selection.value
  if (!current || current.kind !== 'node') return null
  return model.value.nodes[current.index] ?? null
})

const selectedRoute = computed<RouteView | null>(() => {
  const current = selection.value
  if (!current || current.kind !== 'route') return null
  return model.value.routes.find((item) => item.key === current.key) ?? null
})

/** 404 = 这个单元已经不在了（课程库重扫过旧 id 会消失）：给一句人话，而不是把后端码抛到脸上。 */
function graphErrorText(caught: unknown): string {
  if (caught instanceof ApiError && caught.status === 404) {
    return '这个单元已经不在课程库里了（课程库可能被重新扫描过），回课程列表重新进入一次。'
  }
  return errorText(caught)
}

async function load() {
  loading.value = true
  error.value = ''
  selection.value = null
  try {
    const data = await api.getUnitGraph(unitId.value, activeProfile.value?.id || undefined)
    graph.value = data
    live.value = normalizeLive(data?.live)
    model.value = data && data.ok !== false ? buildModel(data) : emptyModel()
  } catch (caught) {
    error.value = graphErrorText(caught)
    graph.value = null
    model.value = emptyModel()
  } finally {
    loading.value = false
  }
  // 首屏与「重新读取」共用：拿不到真正的走向图时它自己不会起定时器。
  startPolling()
}

/* ── 轮询：只取 live ─────────────────────────────────────────────────────
 * 节奏只有一档：拿到真正的走向图（ok=true 且有格子）时 5 秒一次。
 * 降级态（ok=false）**完全不轮询** —— 后端那条降级路径要现场装配课件：
 * 没备过课的单元第一次会解析 pptx（写 deck.json 缓存），可能耗时数秒；
 * 解析不出来（没 pptx / 坏文件）则每次都重来。每 5 秒打一次就是反复挨这一刀，
 * 所以降级、空态、出错一律只留「重新读取」按钮手动触发。 */
let poller = 0
let polling = false

function stopPolling() {
  if (poller) window.clearInterval(poller)
  poller = 0
}

/** 能不能轮询：拿到真正的走向图、而且没有致命错误（404 之后就别再打了）。 */
function pollingAllowed(): boolean {
  return hasGraph.value && !error.value
}

function startPolling() {
  // 先停旧的再决定要不要起新的：加载完、切回前台、重试这三条路径都不会留下两个定时器。
  stopPolling()
  if (!pollingAllowed()) return
  poller = window.setInterval(() => void pollLive(), POLL_MS)
}

async function pollLive() {
  // 页面在后台就什么都不做：切回来时 visibilitychange 会补一次，
  // 隐藏期间每 5 秒发一次请求只是白烧后端。
  if (polling || document.hidden) return
  if (!pollingAllowed()) return
  polling = true
  try {
    const data = await api.getUnitGraph(unitId.value, activeProfile.value?.id || undefined)
    if (!data || data.ok === false) return
    live.value = normalizeLive(data.live)
  } catch (caught) {
    // 404 是「这个单元已经不在了」这种硬错误（课程库重扫后旧 id 会消失）。
    // 继续每 5 秒重试只会把服务端日志刷满，所以停下来，把状态交回页面 + 给一个「重新读取」。
    // 断网 / 5xx 仍然静默跳过：那种抖一下自己会好，不该在页面上闪红字。
    if (caught instanceof ApiError && caught.status === 404) {
      stopPolling()
      error.value = graphErrorText(caught)
      return
    }
  } finally {
    polling = false
  }
}

function onVisibilityChange() {
  if (document.hidden) {
    stopPolling()
    return
  }
  startPolling()
  void pollLive()
}

function onKeyDown(event: KeyboardEvent) {
  if (event.key === 'Escape' && selection.value) selection.value = null
}

onMounted(async () => {
  document.addEventListener('visibilitychange', onVisibilityChange)
  window.addEventListener('keydown', onKeyDown)
  await load()
})

onBeforeUnmount(() => {
  stopPolling()
  document.removeEventListener('visibilitychange', onVisibilityChange)
  window.removeEventListener('keydown', onKeyDown)
})

function pickNode(index: number) {
  selection.value = { kind: 'node', index }
}

function pickRoute(key: string) {
  selection.value = { kind: 'route', key }
}
</script>

<template>
  <div class="unit-graph">
    <RouterLink class="ug-back" :to="outlineLink">
      <svg viewBox="0 0 24 24" aria-hidden="true"><path d="m15 18-6-6 6-6M9 12h11" /></svg>
      返回单元大纲
    </RouterLink>

    <header class="ug-head">
      <span class="ug-eyebrow">LESSON MAP · 课堂走向图</span>
      <h1>{{ title }}</h1>
      <p class="ug-sub">讲解主线 · 预置分支路线 · 当前讲到哪（只读）</p>
    </header>

    <div v-if="loading" class="ug-loading" role="status">
      <span class="ug-loading-bar" aria-hidden="true" />
      <span>正在读取这节课的走向…</span>
    </div>

    <section v-else-if="error" class="ug-panel">
      <span class="ug-panel-mark" aria-hidden="true">!</span>
      <h2>走向图没有打开</h2>
      <p>{{ error }}</p>
      <button class="btn btn-primary" type="button" @click="load">重新读取</button>
    </section>

    <section v-else-if="!ok" class="ug-panel ug-panel--inline">
      <span class="ug-panel-mark" aria-hidden="true">i</span>
      <h2>这节课的走向图暂时画不出来</h2>
      <p>{{ hintText }}</p>
      <button class="btn" type="button" @click="load">重新读取</button>
    </section>

    <section v-else-if="!hasGraph" class="ug-panel ug-panel--inline">
      <span class="ug-panel-mark" aria-hidden="true">·</span>
      <h2>这节课还没有讲解单元</h2>
      <p>备一次课、拿到讲解计划之后，这里会出现每一格的卡片、分支路线和当前进度。</p>
    </section>

    <template v-else>
      <div class="ug-toolbar">
        <div class="ug-actions">
          <button class="ug-btn" type="button" aria-label="适应全课：把整节课缩进画布" @click="canvas?.fit()">
            适应全课
          </button>
          <button class="ug-btn" type="button" aria-label="缩放到 100%" @click="canvas?.actual()">100%</button>
          <!-- 不再因为「暂时没有指针」而变灰：没有当前格就以第一格为锚点，
               按钮永远点得动、永远看得见效果（低于 100% 时它还会先把缩放提到 100%）。 -->
          <button class="ug-btn" type="button" aria-label="回到当前格（不足 100% 时先放大到 100%）" @click="canvas?.focusCurrent()">
            回到当前
          </button>
          <span class="ug-zoom" aria-live="polite">{{ Math.round(zoom * 100) }}%</span>
        </div>
        <ul class="ug-stats">
          <li v-for="item in stats" :key="item">{{ item }}</li>
        </ul>
        <span class="ug-state" :class="{ 'is-live': live.running }">
          <span class="ug-state-dot" aria-hidden="true" />
          {{ statusText }}
        </span>
      </div>

      <GraphCanvas
        ref="canvas"
        :model="model"
        :current-index="currentIndex"
        :current-text="currentText"
        :selected-node="selection?.kind === 'node' ? selection.index : null"
        :selected-route="selection?.kind === 'route' ? selection.key : null"
        @zoom="zoom = $event"
        @pannable="pannable = $event"
        @select-node="pickNode"
        @select-route="pickRoute"
      />

      <p class="ug-tip">
        {{ pannable
          ? '拖动可平移 · 点卡片看这一格，点路线上的小标签看这条支线 · Esc 关面板'
          : '整节课已在画布内，点 100% 放大后可拖动 · 点卡片看这一格，点路线上的小标签看这条支线' }}
      </p>
    </template>

    <GraphDetail :node="selectedNode" :route="selectedRoute" @close="selection = null" />
  </div>
</template>
