<script setup lang="ts">
/**
 * 课件重绘（Vue 版）。
 * 参考画布固定 1280×720（EMU 按 96dpi 换算），整块 transform: scale() 自适应容器，
 * 这样文字换行是确定性的：字号 px = pt × 4/3，与 PowerPoint 96dpi 一致。
 *
 * 原 React 版把节点拆成 ElementNode / TextLayer / PictureNode / TableNode /
 * CellNode / SvgNode 几个子组件；单文件 SFC 里按同样的分支顺序内联，
 * 下面每个函数对应其中一个子组件，方便与 SlideView.tsx 逐行对照。
 */
import { computed, nextTick, onBeforeUnmount, onMounted, reactive, ref, useId, watch, type CSSProperties } from 'vue'
import type {
  DeckBox,
  DeckCell,
  DeckElement,
  DeckGradientStop,
  DeckPara,
  DeckPic,
  DeckRun,
  DeckSlide,
  DeckTable,
} from './types'
import {
  clipFor,
  cssColor,
  fillCss,
  fontStack,
  lineCss,
  paraBaseStyle,
  paraSizePt,
  paraStyle,
  resolveImageSrc,
  roundedStyle,
  runStyle,
} from './style'

const props = defineProps<{
  slide: DeckSlide | null
  /** 当前讲解锚点（元素 id 或段落 id）：随 segment_start.marker 逐句移动。 */
  anchor?: string | null
  /** 本页翻页时应先藏起来的 id（page.reveal） */
  hidden?: string[]
  /** 已经讲出来（step/reveal 过）的 id */
  revealed?: string[]
}>()

/* ---- useScale：1280×720 逻辑画布按容器缩放 ---- */

const width = computed(() => props.slide?.width ?? props.slide?.width_px ?? 1280)
const height = computed(() => props.slide?.height ?? props.slide?.height_px ?? 720)
const mediaBase = computed(() => props.slide?.media_base ?? '')

const hostEl = ref<HTMLDivElement | null>(null)
const scale = ref(0)
let observer: ResizeObserver | null = null

const measure = (): void => {
  const host = hostEl.value
  if (!host) return
  const rect = host.getBoundingClientRect()
  if (!rect.width || !rect.height) return
  scale.value = Math.min(rect.width / width.value, rect.height / height.value)
}

onMounted(() => {
  const host = hostEl.value
  if (!host) return
  measure()
  observer = new ResizeObserver(measure)
  observer.observe(host)
  scheduleMarker()
})

// 逻辑尺寸变了要重新量一次（对应原 useLayoutEffect 的依赖数组）
watch([width, height], () => measure())

onBeforeUnmount(() => {
  observer?.disconnect()
  observer = null
})

const canvasStyle = computed<CSSProperties>(() => ({
  // Vue 不会像 React 那样给样式里的数字补 px，长度一律显式带单位。
  width: width.value + 'px',
  height: height.value + 'px',
  background: props.slide ? fillCss(props.slide.background) || '#fff' : '#fff',
  transform: `scale(${scale.value || 0.0001})`,
  visibility: scale.value ? 'visible' : 'hidden',
}))

/** 版式/母版装饰先画、slide 元素后画：与原组件两个 .map() 的 DOM 顺序一致。 */
const elementGroups = computed(() => [props.slide?.decorations ?? [], props.slide?.elements ?? []])

/* ---- 分步显示 + 逐句光标（课堂运行时） ---- */

/** 该 id 是否还在「本页尚未讲到」的隐藏集合里。 */
const isHidden = (id?: string | null): boolean =>
  Boolean(id && props.hidden?.includes(id) && !props.revealed?.includes(id))

const canvasEl = ref<HTMLDivElement | null>(null)

/** 光标位置（画布坐标系 1280×720；由外层 transform 统一缩放，无需再乘 scale）。 */
const marker = reactive({ x: 0, y: 0, on: false })

const anchorNode = (id: string): HTMLElement | null => {
  const canvas = canvasEl.value
  if (!canvas) return null
  // 段落锚点优先：同一页里段落 id（…-p03）比形状 id 更精确。
  const byPara = canvas.querySelector<HTMLElement>(`[data-anchor-id="${id}"]`)
  if (byPara) return byPara
  return canvas.querySelector<HTMLElement>(`[data-element-id="${id}"]`)
}

const placeMarker = (): void => {
  const id = props.anchor
  const canvas = canvasEl.value
  if (!id || !canvas) {
    marker.on = false
    return
  }
  const node = anchorNode(id)
  if (!node) {
    marker.on = false
    return
  }
  // 被 step 藏起来、还没轮到的元素不指：灯先亮在看不见的地方只会让人以为没高亮。
  if (getComputedStyle(node).visibility === 'hidden') {
    marker.on = false
    return
  }
  const cr = canvas.getBoundingClientRect()
  const nr = node.getBoundingClientRect()
  const s = scale.value || cr.width / width.value || 1
  const x = (nr.left - cr.left) / s
  const y = (nr.top - cr.top) / s
  const h = nr.height / s
  // 落在实际文字行的左侧起点（与老原型 deck.js 的 placeMarker 同一口径），
  // 不压住正文。多行段落按行高取第一行的中线；量不到行高才退回整块高度的 52%。
  const lineHeight = parseFloat(getComputedStyle(node).lineHeight)
  const offset = Number.isFinite(lineHeight) && lineHeight > 0
    ? Math.min(lineHeight * 0.52, 30)
    : Math.min(h * 0.52, 30)
  marker.x = Math.max(10, x - 14)
  marker.y = y + offset
  marker.on = true
}

/** DOM 更新完再量：翻页/揭示/行距变化都会改变锚点位置。 */
const scheduleMarker = (): void => {
  void nextTick(() => {
    placeMarker()
    // 字体异步补齐（SimSun 等本地字体）会改变行盒高度，再补量一次。
    requestAnimationFrame(() => placeMarker())
  })
}

watch(
  [() => props.anchor, () => props.slide, () => props.revealed, scale],
  scheduleMarker,
  { deep: false },
)

/* ---- ElementNode ---- */

const boxOf = (element: DeckElement): DeckBox => element.box ?? [0, 0, 0, 0]

/** 元素外框：定位、尺寸、旋转、z 序（master 0 < layout 1000 < slide 100000）。 */
const elementStyle = (element: DeckElement): CSSProperties => {
  const [left, top, w, h] = boxOf(element)
  const source = element.source === 'master' ? 0 : element.source === 'layout' ? 1000 : 100000
  const style: CSSProperties = {
    position: 'absolute',
    left: (left || 0).toFixed(2) + 'px',
    top: (top || 0).toFixed(2) + 'px',
    width: Math.max(0, w || 0).toFixed(2) + 'px',
    height: Math.max(0, h || 0).toFixed(2) + 'px',
    zIndex: source + (element.z || 0),
  }
  if (element.rot) style.transform = `rotate(${element.rot}deg)`
  return style
}

/** 形状底：填充 + 圆角/椭圆 + 预设裁剪 + 描边，文字层盖在它上面。 */
const shapeStyle = (element: DeckElement): CSSProperties => {
  const [, , w, h] = boxOf(element)
  const style: CSSProperties = {
    position: 'absolute',
    inset: 0,
    background: fillCss(element.fill),
    ...roundedStyle(element.kind, element.preset, w, h),
  }
  const clip = clipFor(element.preset)
  if (clip) style.clipPath = clip
  const border = lineCss(element.line)
  if (border) style.border = border
  return style
}

/* ---- TextLayer ---- */

/** 空段落/空 runs 都不算有文字：原实现就是靠这个决定要不要画文本层。 */
const hasText = (element: DeckElement): boolean =>
  Boolean(
    element.text &&
      (element.text.paras ?? []).some(
        (para) => (para.text || '').length || (para.runs ?? []).length,
      ),
  )

const textParas = (element: DeckElement): DeckPara[] => element.text?.paras ?? []

/** font_scale：解析器给了非 1 的值才缩放。 */
const textScaleOf = (element: DeckElement): number => {
  const fontScale = element.text?.font_scale
  return fontScale && fontScale !== 1 ? fontScale : 1
}

/** 文本层：内边距按原值（normAutofit 只缩字，不缩内边距），anchor 决定垂直对齐。
 *
 * 关键三条：
 * - `overflow: visible`：PowerPoint 的文本框不裁剪溢出的文字，裁了就是「字少一截」；
 * - `white-space: pre`：解析器里 wrap=false 的形状在 PowerPoint 里不自动换行，
 *   只认显式的 <a:br>（解析成 "\n"）。用 normal 会把长句折行、把行数撑多再裁掉；
 * - 内边距不乘 font_scale：那是 normAutofit 的缩字比例，与框内边距无关。
 */
const textLayerStyle = (element: DeckElement): CSSProperties => {
  const text = element.text
  if (!text) return {}
  const insets =
    text.insets_px && text.insets_px.length === 4 ? text.insets_px : [9.6, 4.8, 9.6, 4.8]
  return {
    position: 'absolute',
    inset: 0,
    display: 'flex',
    flexDirection: 'column',
    justifyContent:
      text.anchor === 'middle' ? 'center' : text.anchor === 'bottom' ? 'flex-end' : 'flex-start',
    padding: insets.map((value) => (value || 0).toFixed(2) + 'px').join(' '),
    overflow: 'visible',
    whiteSpace: text.wrap === false ? 'pre' : 'pre-wrap',
  }
}

/** 段落样式：段落默认样式（defRPr）覆盖段落自身的行距/缩进等（顺序同原实现）。 */
const textParaStyle = (para: DeckPara, scaleValue: number): CSSProperties => ({
  ...paraStyle(para, scaleValue),
  ...paraBaseStyle(para, scaleValue, paraSizePt(para)),
})

const paraRuns = (para: DeckPara): DeckRun[] => para.runs ?? []

const isBulletPara = (para: DeckPara): boolean => para.bullet === 'char' || para.bullet === 'auto'

const bulletChar = (para: DeckPara): string => para.bullet_char || '•'

/* ---- PictureNode ---- */

const picOf = (element: DeckElement): DeckPic => element.pic ?? {}

/** media_base 拼接/转义规则原样复用 style.ts 的 resolveImageSrc。 */
const picSrc = (element: DeckElement): string => {
  const pic = picOf(element)
  return resolveImageSrc(pic.src, pic.external_url, mediaBase.value)
}

const picAlt = (element: DeckElement): string => picOf(element).alt || element.name || ''

/** 加载失败按 element_id 记，等价于原组件里按 key 复用的 useState。 */
const picFailed = ref<Record<string, boolean>>({})
const isPicFailed = (element: DeckElement): boolean => Boolean(picFailed.value[element.element_id])
const onPicError = (element: DeckElement): void => {
  picFailed.value[element.element_id] = true
}

const missingPicText = (element: DeckElement): string => {
  const src = picSrc(element)
  return src ? '图片未取到：' + src.slice(0, 60) : '图片缺失'
}

const picWrapStyle = (element: DeckElement): CSSProperties => {
  const style: CSSProperties = {
    position: 'absolute',
    inset: 0,
    background: isPicFailed(element) ? undefined : '#eef1f6',
    display: 'flex',
    alignItems: 'center',
    justifyContent: 'center',
    overflow: 'hidden',
  }
  const border = lineCss(element.line)
  if (border) style.border = border
  return style
}

/**
 * 裁剪：crop = [左, 上, 右, 下]（0~1，是"要裁掉"的比例）。
 * 按保留区域把图放大到 100/kw，再用负 margin 把裁掉的部分推出可视框。
 */
const picImageStyle = (element: DeckElement): CSSProperties => {
  const style: CSSProperties = { width: '100%', height: '100%', objectFit: 'fill', display: 'block' }
  const crop = picOf(element).crop ?? [0, 0, 0, 0]
  if (crop.some((value) => Boolean(value))) {
    const [l, t, r, b] = crop
    const kw = 1 - l - r
    const kh = 1 - t - b
    style.clipPath = `inset(${t * 100}% ${r * 100}% ${b * 100}% ${l * 100}%)`
    style.width = `${100 / kw}%`
    style.height = `${100 / kh}%`
    style.marginLeft = `${(-l / kw) * 100}%`
    style.marginTop = `${(-t / kh) * 100}%`
    style.position = 'absolute'
  }
  return style
}

/* ---- TableNode / CellNode ---- */

const tableOf = (element: DeckElement): DeckTable => element.table ?? {}
const colWidths = (element: DeckElement): number[] => tableOf(element).col_w_px ?? []
const cellRows = (element: DeckElement): DeckCell[][] => tableOf(element).cells ?? []
const cellParas = (cell: DeckCell): DeckPara[] => cell.paras ?? []

/** 行高只在给了正数时写 height，0 当作没给（与原实现的三元判断一致）。 */
const rowStyle = (element: DeckElement, rowIndex: number): CSSProperties | undefined => {
  const rowHeight = tableOf(element).row_h_px?.[rowIndex]
  return rowHeight ? { height: rowHeight + 'px' } : undefined
}

const cellStyle = (cell: DeckCell): CSSProperties => {
  const style: CSSProperties = {
    border: '1px solid #d8e0e8',
    verticalAlign: 'middle',
    padding: '4px 8px',
  }
  const fill = cell.fill
  if (fill && fill.kind !== 'none') style.background = fillCss(fill)
  return style
}

/** 单元格里的段落：合并顺序与原组件相反（paraStyle 覆盖 paraBaseStyle）。 */
const cellParaStyle = (para: DeckPara): CSSProperties => ({
  ...paraBaseStyle(para, 1, paraSizePt(para)),
  ...paraStyle(para, 1),
})

/* ---- SvgNode ---- */

// 渐变 id 必须文档内唯一：useId 给出组件实例前缀，再按 element_id 缓存，
// 保证重绘时 id 不变（原组件用 useId 的同一套思路）。
const uid = useId().replace(/:/g, '')
const gradientIds = new Map<string, string>()

const gradientId = (element: DeckElement): string => {
  const cached = gradientIds.get(element.element_id)
  if (cached) return cached
  const next = `${uid}-g${gradientIds.size}`
  gradientIds.set(element.element_id, next)
  return next
}

const gradientStops = (element: DeckElement): DeckGradientStop[] => element.fill?.gradient ?? []

const usesGradient = (element: DeckElement): boolean => {
  const fill = element.fill
  return Boolean(fill && fill.kind === 'gradient' && fill.gradient && fill.gradient.length)
}

/** 角度 -> 渐变线端点（0° 向上，与解析器换算后的 CSS 约定一致）。 */
const gradientVector = (
  element: DeckElement,
): { x1: number; y1: number; x2: number; y2: number } => {
  const radians = ((element.fill?.angle || 0) * Math.PI) / 180
  const dx = Math.sin(radians) / 2
  const dy = Math.cos(radians) / 2
  return { x1: 0.5 - dx, y1: 0.5 + dy, x2: 0.5 + dx, y2: 0.5 - dy }
}

const stopOffset = (stop: DeckGradientStop): string => `${Math.round((stop.pos || 0) * 100)}%`

const svgFill = (element: DeckElement): string => {
  const fill = element.fill
  if (usesGradient(element)) return `url(#${gradientId(element)})`
  return fill && fill.kind !== 'none' && fill.kind !== 'image' ? fillCss(fill) : 'none'
}

const svgStroke = (element: DeckElement): string => {
  const line = element.line
  return line && line.kind !== 'none' && line.width_px ? cssColor(line.color, line.alpha) : 'none'
}

const strokeWidthOf = (element: DeckElement): number | undefined => {
  const line = element.line
  return line && line.width_px ? line.width_px : undefined
}

const lineCap = (element: DeckElement): 'round' | 'butt' =>
  element.line?.cap === 'round' ? 'round' : 'butt'

/** 虚线：dash 里带 dot 按点线画，其余一律 6 4。 */
const dashArray = (element: DeckElement): string | undefined => {
  const dash = element.line?.dash
  if (!dash) return undefined
  return dash.indexOf('dot') >= 0 ? '2 3' : '6 4'
}

/** SVG 的 viewBox/端点用元素原始宽高（负值也保留，viewBox 时再夹到 ≥1）。 */
const svgSize = (element: DeckElement): { w: number; h: number } => {
  const [, , w, h] = boxOf(element)
  return { w, h }
}
</script>

<template>
  <div ref="hostEl" class="deck-host">
    <div v-if="!slide" class="deck-placeholder">
      <span>正在载入课件…</span>
    </div>
    <div v-else ref="canvasEl" class="deck-canvas" :style="canvasStyle">
      <template v-for="(group, groupIndex) in elementGroups" :key="groupIndex">
        <template v-for="element in group" :key="element.element_id">
          <div
            class="deck-el"
            :class="{ 'is-hidden': isHidden(element.element_id) }"
            :data-element-id="element.element_id"
            :style="elementStyle(element)"
          >
            <!-- picture -->
            <template v-if="element.kind === 'picture'">
              <div
                :style="picWrapStyle(element)"
                :class="isPicFailed(element) ? 'deck-pic-missing' : undefined"
              >
                <span v-if="!picSrc(element) || isPicFailed(element)" class="deck-unrender">{{ missingPicText(element) }}</span>
                <img
                  v-else
                  :src="picSrc(element)"
                  :alt="picAlt(element)"
                  loading="lazy"
                  :style="picImageStyle(element)"
                  @error="onPicError(element)"
                />
              </div>
            </template>

            <!-- table -->
            <template v-else-if="element.kind === 'table'">
              <div class="deck-el" style="position: absolute; inset: 0">
                <table
                  class="deck-table"
                  style="width: 100%; height: 100%; border-collapse: collapse; table-layout: fixed"
                >
                  <colgroup>
                    <col
                      v-for="(colWidth, colIndex) in colWidths(element)"
                      :key="colIndex"
                      :style="{ width: colWidth + 'px' }"
                    />
                  </colgroup>
                  <tbody>
                    <tr
                      v-for="(row, rowIndex) in cellRows(element)"
                      :key="rowIndex"
                      :style="rowStyle(element, rowIndex)"
                    >
                      <td
                        v-for="(cell, cellIndex) in row"
                        :key="cellIndex"
                        :colspan="cell.col_span && cell.col_span > 1 ? cell.col_span : undefined"
                        :rowspan="cell.row_span && cell.row_span > 1 ? cell.row_span : undefined"
                        :style="cellStyle(cell)"
                      >
                        <template v-if="cellParas(cell).length">
                          <div
                            v-for="(para, paraIndex) in cellParas(cell)"
                            :key="para.pid || paraIndex"
                            :data-anchor-id="para.pid"
                            :class="{ 'is-hidden': isHidden(para.pid) }"
                            :style="cellParaStyle(para)"
                          >
                            <span
                              v-for="(run, runIndex) in paraRuns(para)"
                              :key="runIndex"
                              :style="runStyle(run, 1)"
                            >{{ run.text || '' }}</span>
                            <template v-if="!paraRuns(para).length">{{ para.text }}</template>
                          </div>
                        </template>
                        <template v-else>{{ cell.text || '' }}</template>
                      </td>
                    </tr>
                  </tbody>
                </table>
              </div>
            </template>

            <!-- freeform / line / connector -->
            <template
              v-else-if="element.kind === 'freeform' || element.kind === 'line' || element.kind === 'connector'"
            >
              <svg
                :viewBox="`0 0 ${Math.max(1, svgSize(element).w)} ${Math.max(1, svgSize(element).h)}`"
                preserveAspectRatio="none"
                style="position: absolute; inset: 0; width: 100%; height: 100%; overflow: visible"
              >
                <defs v-if="usesGradient(element)">
                  <linearGradient :id="gradientId(element)" v-bind="gradientVector(element)">
                    <stop
                      v-for="(stop, stopIndex) in gradientStops(element)"
                      :key="stopIndex"
                      :offset="stopOffset(stop)"
                      :stop-color="cssColor(stop.color, stop.alpha)"
                    />
                  </linearGradient>
                </defs>
                <path
                  v-if="element.svg_path"
                  :d="element.svg_path"
                  :fill="svgFill(element)"
                  :stroke="svgStroke(element)"
                  :stroke-width="strokeWidthOf(element)"
                  stroke-linejoin="round"
                  :stroke-linecap="lineCap(element)"
                  :stroke-dasharray="dashArray(element)"
                />
                <line
                  v-else
                  :x1="0"
                  :y1="0"
                  :x2="svgSize(element).w"
                  :y2="svgSize(element).h"
                  :stroke="svgStroke(element) === 'none' ? '#888' : svgStroke(element)"
                  :stroke-width="strokeWidthOf(element) || 1"
                />
              </svg>
            </template>

            <!-- 其余（text / rect / 占位符 / unknown …） -->
            <template v-else>
              <div :style="shapeStyle(element)" />
              <div v-if="hasText(element)" :style="textLayerStyle(element)">
                <div
                  v-for="(para, paraIndex) in textParas(element)"
                  :key="para.pid || paraIndex"
                  :data-anchor-id="para.pid"
                  :class="{ 'is-hidden': isHidden(para.pid) }"
                  :style="textParaStyle(para, textScaleOf(element))"
                >
                  <span
                    v-if="isBulletPara(para)"
                    class="deck-bullet"
                    :style="{ fontFamily: fontStack(para.default_font) }"
                  >{{ bulletChar(para) }}</span>
                  <template v-if="paraRuns(para).length">
                    <span
                      v-for="(run, runIndex) in paraRuns(para)"
                      :key="runIndex"
                      :style="runStyle(run, textScaleOf(element))"
                    >{{ run.text || '' }}</span>
                  </template>
                  <template v-else>{{ para.text || '' }}</template>
                </div>
              </div>
              <div v-if="element.kind === 'unknown'" class="deck-unrender">未渲染：{{ element.name || element.preset || element.kind }}</div>
            </template>
          </div>
        </template>
      </template>

      <!-- 逐句光标：红色脉冲点，落在当前这句的锚点文字左侧（老原型的 #marker） -->
      <div
        class="deck-marker"
        :class="{ 'is-on': marker.on }"
        :style="{ left: marker.x + 'px', top: marker.y + 'px' }"
        aria-hidden="true"
      />
    </div>
  </div>
</template>
