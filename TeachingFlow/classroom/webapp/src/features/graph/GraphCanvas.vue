<script setup lang="ts">
import { computed, onBeforeUnmount, onMounted, ref, watch } from 'vue'
import { CARD_H, CARD_W, COMPACT_K, MAX_K, MIN_K, clamp } from './geometry'
import { displayNodeNo, type GraphModel } from './model'

/**
 * 走向图画布：只负责「怎么显示」（缩放 / 平移 / 命中），数据由页面传进来。
 *
 * 缩放平移全在一层 transform 上做，卡片的 left/top 永远是布局算死的定值 ——
 * 指针每 5 秒刷新只改传进来的 currentIndex，这里的几何一个字都不会动。
 */
const props = defineProps<{
  model: GraphModel
  currentIndex: number | null
  /** 指针气泡上的文案（「你现在在这里」/「上次讲到第 N 页」）。 */
  currentText: string
  selectedNode: number | null
  selectedRoute: string | null
}>()

const emit = defineEmits<{
  'select-node': [index: number]
  'select-route': [key: string]
  zoom: [value: number]
  /** 现在这幅图在画布内是否还有可平移的空间（页面据此决定提示语）。 */
  pannable: [value: boolean]
}>()

/** 气泡在屏幕上的半宽（与 graph.css 的 .ug-bubble 一致）：只用来判断它会不会出界。 */
const BUBBLE_EDGE = 132

const viewport = ref<HTMLElement | null>(null)
const k = ref(1)
const tx = ref(0)
const ty = ref(0)
const vw = ref(0)
const vh = ref(0)
const dragging = ref(false)
const hoverRoute = ref('')

const compact = computed(() => k.value < COMPACT_K)
const transform = computed(() => `translate(${tx.value}px, ${ty.value}px) scale(${k.value})`)

/**
 * 这幅图现在能不能拖：内容缩放后是否超出画布。
 * 不能拖的时候要**诚实** —— 不显示 grab 手型、不挂起拖动、提示语直说「已在画布内」，
 * 否则用户拖一下发现纹丝不动，只会以为页面坏了（这正是「适应全课」那一档的老问题）。
 * 1px 容差：fit 的 k 是算出来的，缩放回来会差个浮点尾数，差不到 1px 就别假装能拖。
 */
const canPan = computed(
  () => props.model.width * k.value > vw.value + 1 || props.model.height * k.value > vh.value + 1,
)

/** 指针锚点：画布坐标 → 屏幕坐标。气泡与竖线画在屏幕层，缩到 2% 也还读得出来。 */
const anchorX = computed(() => {
  const index = props.currentIndex
  if (index == null) return 0
  const node = props.model.nodes[index]
  if (!node) return 0
  return tx.value + (node.x + CARD_W / 2) * k.value
})
const anchorY = computed(() => ty.value + (props.model.cardTop) * k.value)
const pointerVisible = computed(() => {
  const index = props.currentIndex
  return index != null && !!props.model.nodes[index] && vw.value > 0 && anchorX.value >= 0 && anchorX.value <= vw.value
})
const bubbleVisible = computed(
  () => pointerVisible.value && anchorX.value >= BUBBLE_EDGE && anchorX.value <= vw.value - BUBBLE_EDGE,
)
/** 卡片贴到画布顶上时气泡翻到卡片下面：翻一下比被裁掉一条强。 */
const bubbleBelow = computed(() => anchorY.value < 44)
const bubbleTop = computed(() =>
  bubbleBelow.value ? ty.value + (props.model.mainY + CARD_H / 2) * k.value + 8 : anchorY.value - 8,
)

function measure() {
  const el = viewport.value
  if (!el) return
  vw.value = el.clientWidth
  vh.value = el.clientHeight
  if (!vw.value) return
  if (!fitted) {
    fitted = true
    initialView()
    return
  }
  // 画布尺寸变了要重算「适应全课」—— 页面现在是视口内定高的列布局，窗口一改高度，
  // 视口高度跟着变，k 不跟着算就会留白或裁掉两边。
  // 但只在「还停在适应全课这一档」时重算：用户手动缩到 100% 后拉一下窗口被弹回全景，那种帮忙很讨厌。
  if (Math.abs(k.value - fitK) < 1e-6) fit()
  else clampPan()
}

/**
 * 平移夹紧：内容比视口小就居中，比视口大就夹在两端。
 * 这条是「拖动可达两端」的保证：拖到底正好停在第一格/最后一格的边上，不会拖丢。
 */
function clampPan() {
  const scaledW = props.model.width * k.value
  const scaledH = props.model.height * k.value
  tx.value = scaledW <= vw.value ? (vw.value - scaledW) / 2 : clamp(tx.value, vw.value - scaledW, 0)
  ty.value = scaledH <= vh.value ? (vh.value - scaledH) / 2 : clamp(ty.value, vh.value - scaledH, 0)
}

/** 以某个锚点（默认视口中心）缩放：中心那一格缩放后还在中心，图不会跳走。 */
function zoomTo(next: number, anchorPx = vw.value / 2, anchorPy = vh.value / 2) {
  if (!vw.value || !vh.value) return
  const target = clamp(next, MIN_K, MAX_K)
  const ratio = target / k.value
  tx.value = anchorPx - (anchorPx - tx.value) * ratio
  ty.value = anchorPy - (anchorPy - ty.value) * ratio
  fitK = -1 // 手动换过缩放档：之后视口尺寸变化只夹平移，不再重算适应全课
  k.value = target
  clampPan()
  emit('zoom', target)
}

/**
 * 默认视图：100% + 把当前格摆到画布中间。
 *
 * 为什么默认不落在「适应全课」：fit 的定义就是 min(vw/W, vh/H, 1)，内容永远装得下，
 * 于是拖动会被夹回原位、回到当前也无处可去 —— 打开页面就拖不动，用户会判定这页坏了。
 * 默认档必须是能拖能点的档；「整节课长什么样」由「适应全课」一键提供。
 */
function initialView() {
  if (!vw.value) return
  zoomTo(1)
  centerOn(props.currentIndex ?? 0)
}

/** 把某一格摆到画布正中（只改平移，不动缩放）。 */
function centerOn(index: number) {
  const node = props.model.nodes[index]
  if (!node || !vw.value || !vh.value) return
  tx.value = vw.value / 2 - (node.x + CARD_W / 2) * k.value
  ty.value = vh.value / 2 - (props.model.mainY - CARD_H / 2) * k.value
  clampPan()
}

/** 适应全课：整节课（含所有泳道）装进视口。177 格的课在这一档大约是 2%，仍是有限的宽度。 */
function fit() {
  if (!vw.value || !vh.value) return
  const target = clamp(Math.min(vw.value / Math.max(1, props.model.width), vh.value / Math.max(1, props.model.height), 1), MIN_K, MAX_K)
  fitK = target
  k.value = target
  clampPan()
  emit('zoom', target)
}

function actual() {
  zoomTo(1)
}

/**
 * 回到当前：**每次点都必须看得见效果**。
 * 缩放低于 100% 时先提到 100% 再居中 —— 在 fit 档只调平移等于没反应（平移无处可去）；
 * 已经是 100% 或更大时只居中，不动用户的阅读距离。
 * 没有指针（课堂没在跑过）就以第一格为锚点，而不是把按钮变成点了没用。
 */
function focusCurrent() {
  if (!vw.value || !props.model.nodes.length) return
  if (k.value < 1) zoomTo(1)
  centerOn(props.currentIndex ?? 0)
  // 已经居中的时候位置不会再变（默认视图本来就是「当前格在正中」），
  // 那就用一圈脉冲告诉用户「你点的就是这里」—— 按钮永远不许表现得像坏了。
  pulseCurrent()
}

defineExpose({ fit, actual, focusCurrent })

/** 当前格那一圈提示动画：只在「点了回到当前」时亮一下，不参与任何几何。 */
const pulsing = ref(false)
let pulseTimer = 0

function pulseCurrent() {
  window.clearTimeout(pulseTimer)
  pulsing.value = false
  // 下一帧再加 class，否则连点两次时浏览器不会重启动画
  requestAnimationFrame(() => {
    pulsing.value = true
    pulseTimer = window.setTimeout(() => {
      pulsing.value = false
    }, 700)
  })
}

onBeforeUnmount(() => {
  window.clearTimeout(pulseTimer)
})

let fitted = false
/** 上一次「适应全课」算出的 k：等于当前 k 就说明用户还没手动改过缩放档。 */
let fitK = 0
let observer: ResizeObserver | null = null

onMounted(() => {
  const el = viewport.value
  if (!el) return
  observer = new ResizeObserver(() => measure())
  observer.observe(el)
  measure()
})

onBeforeUnmount(() => {
  observer?.disconnect()
  observer = null
})

// 换了一份数据（重试 / 换单元）就回到默认视图（100% + 当前格居中）；轮询只改 live，进不到这里。
watch(
  () => props.model,
  () => {
    if (vw.value > 0) initialView()
  },
)

// 能不能拖交给页面：拖不动的时候提示语要直说「整节课已在画布内」，而不是让人拖了才发现是空的。
watch(canPan, (value) => emit('pannable', value), { immediate: true })

/* ── 拖动平移：pointer 事件一套走完鼠标与触摸，不做滚轮缩放（缩放只有工具条的三个按钮） ── */
interface DragState {
  pointerId: number
  startX: number
  startY: number
  fromTx: number
  fromTy: number
}

let drag: DragState | null = null
/** 这一次手势是否真的拖动了：拖动结束时浏览器仍会补一个 click，不拦就会误选卡片。 */
let moved = false

function onPointerDown(event: PointerEvent) {
  if (event.pointerType === 'mouse' && event.button !== 0) return
  // 内容整个都在画布内就别挂起拖动手势：夹紧后位移恒为 0，只会让人以为拖坏了。
  if (!canPan.value) return
  // 这里**不**抓指针：setPointerCapture 会把 pointerup/click 改派给画布本身，
  // 卡片上的点击就永远收不到了（点卡片没反应）。等真的拖起来再抓。
  drag = { pointerId: event.pointerId, startX: event.clientX, startY: event.clientY, fromTx: tx.value, fromTy: ty.value }
  moved = false
  dragging.value = true
}

function onPointerMove(event: PointerEvent) {
  if (!drag || event.pointerId !== drag.pointerId) return
  const dx = event.clientX - drag.startX
  const dy = event.clientY - drag.startY
  if (!moved && Math.abs(dx) + Math.abs(dy) <= 4) return
  if (!moved) {
    moved = true
    viewport.value?.setPointerCapture(event.pointerId)
  }
  tx.value = drag.fromTx + dx
  ty.value = drag.fromTy + dy
  clampPan()
}

function onPointerUp(event: PointerEvent) {
  if (!drag || event.pointerId !== drag.pointerId) return
  const el = viewport.value
  if (el?.hasPointerCapture(event.pointerId)) el.releasePointerCapture(event.pointerId)
  drag = null
  dragging.value = false
}

/** 还没真正拖起来就把指针移出画布：手势作废，别让光标一直停在 grabbing 上。 */
function onPointerLeave(event: PointerEvent) {
  if (!drag || event.pointerId !== drag.pointerId || moved) return
  drag = null
  dragging.value = false
}

function takeMoved(): boolean {
  const value = moved
  moved = false
  return value
}

function isHot(key: string): boolean {
  return hoverRoute.value === key || props.selectedRoute === key
}

function pickNode(index: number) {
  if (takeMoved()) return
  emit('select-node', index)
}

function pickRoute(key: string) {
  if (takeMoved()) return
  emit('select-route', key)
}
</script>

<template>
  <div
    ref="viewport"
    class="ug-viewport"
    :class="{ 'is-dragging': dragging, 'is-pannable': canPan }"
    role="group"
    aria-label="课堂走向图画布：拖动可平移，卡片与路线可点开详情"
    @pointerdown="onPointerDown"
    @pointermove="onPointerMove"
    @pointerup="onPointerUp"
    @pointercancel="onPointerUp"
    @pointerleave="onPointerLeave"
  >
    <div
      class="ug-canvas"
      :class="{ 'is-compact': compact }"
      :style="{ transform, width: `${model.width}px`, height: `${model.height}px`, '--ug-k': k }"
    >
      <!-- 页标尺：一页一条带，隔页交替底色。缩到很小的时候卡面已经读不出字，
           页与页的分界靠这条带子还看得出来。 -->
      <div class="ug-pages" aria-hidden="true">
        <div
          v-for="band in model.pages"
          :key="band.key"
          class="ug-page"
          :class="{ 'is-alt': band.alt }"
          :style="{ left: `${band.left}px`, width: `${band.width}px` }"
        >
          <span class="ug-page-label">第 {{ band.page }} 页 · {{ band.title || '未命名页' }}</span>
        </div>
      </div>

      <svg class="ug-wires" :width="model.width" :height="model.height" aria-hidden="true">
        <path class="ug-mainline" :d="model.mainLine" />
        <g
          v-for="route in model.routes"
          :key="route.key"
          class="ug-wire"
          :class="[`is-${route.kind}`, { 'is-hot': isHot(route.key) }]"
        >
          <!-- 可见线只有 2px，单独铺一条 16px 的透明命中线，鼠标才点得中 -->
          <path
            class="ug-wire-hit"
            :d="route.d"
            @pointerenter="hoverRoute = route.key"
            @pointerleave="hoverRoute = ''"
            @click="pickRoute(route.key)"
          />
          <path class="ug-wire-line" :d="route.d" />
        </g>
      </svg>

      <button
        v-for="node in model.nodes"
        :key="node.index"
        type="button"
        class="ug-node"
        :class="{
          'has-branch': node.hasBranch,
          'is-current': node.index === currentIndex,
          'is-selected': node.index === selectedNode,
          'is-page-end': node.pageEnd,
          'is-pulse': pulsing && node.index === (currentIndex ?? 0),
        }"
        :style="{ left: `${node.x}px`, top: `${model.cardTop}px` }"
        :aria-current="node.index === currentIndex ? 'step' : undefined"
        :aria-label="`${node.page > 0 ? `第 ${node.page} 页` : '页号缺'} 第 ${displayNodeNo(node.n)} 格：${node.summary || node.pageTitle || '没有摘要'}`"
        @click="pickNode(node.index)"
      >
        <span class="ug-node-tag">{{ node.page > 0 ? `第 ${node.page} 页` : '页号缺' }} · 格 {{ displayNodeNo(node.n) }}</span>
        <span class="ug-node-sum">{{ node.summary || '（这一格没有摘要）' }}</span>
      </button>

      <button
        v-for="route in model.routes"
        :key="`chip-${route.key}`"
        type="button"
        class="ug-chip"
        :class="[`is-${route.kind}`, { 'is-hot': isHot(route.key) }]"
        :style="{ left: `${route.chipX}px`, top: `${route.chipY}px` }"
        :title="route.summary || route.label"
        :aria-label="`${route.kindLabel}：${route.label || '未命名路线'}，跨 ${route.span} 格，点击看详情`"
        @pointerenter="hoverRoute = route.key"
        @pointerleave="hoverRoute = ''"
        @click="pickRoute(route.key)"
      >
        {{ route.chipText }}
      </button>
    </div>

    <!-- 指针画在屏幕层：不跟着缩放变小，缩到「适应全课」也还读得出「你现在在这里」 -->
    <div v-if="pointerVisible" class="ug-pointer" :style="{ left: `${anchorX}px` }" aria-hidden="true" />
    <div
      v-if="bubbleVisible"
      class="ug-bubble"
      :class="{ 'is-below': bubbleBelow }"
      :style="{ left: `${anchorX}px`, top: `${bubbleTop}px` }"
    >
      {{ currentText }}
    </div>
  </div>
</template>
