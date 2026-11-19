/**
 * 接口载荷 → 页面模型。
 *
 * 后端在这条接口上是并行开发的，字段缺失、为 null、数组变对象都可能发生；
 * 这里把每个字段都过一遍（文本按空串、数字按 0、数组按空数组），
 * 于是一个脏字段最多让某一格显示成空白，不会让整页白掉。
 */
import type { GraphLive, GraphNode, GraphRoute, UnitGraph } from '@/api/types'
import {
  assignLanes,
  buildGeometry,
  mainLinePath,
  pageBands,
  routeWire,
  type PageBand,
} from './geometry'

export interface NodeView {
  index: number
  n: number
  unitId: string
  slide: number
  page: number
  pageTitle: string
  summary: string
  preview: string
  kp: string[]
  estSec: number
  pageEnd: boolean
  /** 卡片左边缘（画布坐标）。 */
  x: number
  /** 这一格是某条路线的岔出/汇回格：卡片上留个入口点。 */
  hasBranch: boolean
}

export interface StepView {
  unitId: string
  label: string
  text: string
  estSec: number
}

export interface RouteView {
  key: string
  kind: string
  kindLabel: string
  label: string
  fork: number
  merge: number
  /** 岔出格/汇回格的格号（1 起）；汇回课尾时 mergeN 为 null。 */
  forkN: number
  mergeN: number | null
  terminal: boolean
  span: number
  skipped: number
  repeats: number
  estSec: number
  summary: string
  reason: string
  steps: StepView[]
  lane: number
  d: string
  chipX: number
  chipY: number
  chipText: string
}

export interface GraphModel {
  width: number
  height: number
  mainY: number
  cardTop: number
  mainLine: string
  nodes: NodeView[]
  routes: RouteView[]
  pages: PageBand[]
  totals: { nodes: number; routes: number; slides: number; est_sec: number }
}

const KIND_LABELS: Record<string, string> = {
  skip: '跳讲',
  revisit: '补回头',
  finish: '提前收尾',
}

export function routeKindLabel(kind: string): string {
  return KIND_LABELS[kind] || kind || '路线'
}

function text(value: unknown): string {
  return typeof value === 'string' ? value : value == null ? '' : String(value)
}

function num(value: unknown): number {
  const parsed = Number(value)
  return Number.isFinite(parsed) ? parsed : 0
}

function int(value: unknown): number {
  return Math.trunc(num(value))
}

function positive(value: unknown, fallback: number): number {
  const parsed = int(value)
  return parsed > 0 ? parsed : fallback
}

/**
 * 格号是 **0 基**（n === 它在 script.units 里的下标），所以不能借 positive() 兜底 ——
 * 第一格的 n 就是 0，会被当成「没给」而顶成 index + 1，整排编号跟着错一位。
 * 只有缺字段/非法值才退回下标，两者本来就是同一个口径。
 */
function nodeNo(value: unknown, fallback: number): number {
  if (value == null || value === '') return fallback
  const parsed = Number(value)
  return Number.isFinite(parsed) ? Math.trunc(parsed) : fallback
}

/**
 * 界面上的格号 = 数据里的格号 + 1。
 *
 * 接口是 0 基（见 GraphNode.n），人话里「第一格」必须从 1 数；+1 **只在这一个函数里做**，
 * 所有显示点都调它 —— 否则以后有人漏改一处，「第 3 格」就会写成「第 2 格」。
 * 数据、下标、几何、resolveCurrentIndex 一律继续用 0 基。
 */
export function displayNodeNo(n: number): number {
  return n + 1
}

function toList<T>(value: T[] | null | undefined): T[] {
  return Array.isArray(value) ? value : []
}

export function emptyLive(): GraphLive {
  return { running: false, source: 'none', session_id: '', node: null, slide: null, phase: '', comprehension: null }
}

/** 只认 live 这一段：轮询拿回来的其余部分与首屏一样，重复应用没有意义（还容易把几何弄重算一遍）。 */
export function normalizeLive(raw: unknown): GraphLive {
  if (!raw || typeof raw !== 'object') return emptyLive()
  const value = raw as Record<string, unknown>
  const node = value.node == null ? null : int(value.node)
  const slide = value.slide == null ? null : int(value.slide)
  return {
    running: value.running === true,
    source: typeof value.source === 'string' && value.source ? value.source : 'none',
    session_id: text(value.session_id),
    node: Number.isFinite(node) ? node : null,
    slide: Number.isFinite(slide) ? slide : null,
    phase: text(value.phase),
    comprehension: value.comprehension == null ? null : num(value.comprehension),
  }
}

/**
 * 当前格 → 页面数组下标。
 *
 * 为什么按 n 反查、而不是把 live.node 直接当下标用：接口给的是**格号**（0 基，等于 n），
 * 不是这个数组的下标。这一刻两者恰好相等，但节点一旦缺字段、乱序或去重，直接当下标用就会错一格。
 * 反查失败才退回按下标解，再不行返回 null（页面就不画指针，绝不猜）。
 */
export function resolveCurrentIndex(nodes: NodeView[], live: GraphLive): number | null {
  if (live.node == null) return null
  const byNumber = nodes.findIndex((node) => node.n === live.node)
  if (byNumber >= 0) return byNumber
  const index = Math.trunc(live.node)
  return index >= 0 && index < nodes.length ? index : null
}

/** 空模型：页面在没有数据时也拿得到一个非 null 的 model（画布本身由 v-if 挡在外面）。 */
export function emptyModel(): GraphModel {
  const geometry = buildGeometry(0, 0, false)
  return {
    width: geometry.width,
    height: geometry.height,
    mainY: geometry.mainY,
    cardTop: geometry.cardTop,
    mainLine: '',
    nodes: [],
    routes: [],
    pages: [],
    totals: { nodes: 0, routes: 0, slides: 0, est_sec: 0 },
  }
}

export function buildModel(graph: UnitGraph): GraphModel {
  const rawNodes = toList<GraphNode>(graph?.nodes)
  const rawRoutes = toList<GraphRoute>(graph?.routes)
  const count = rawNodes.length

  // 路线端点先归一化：岔出格一定落在主线上、汇回格只能往后走（往回画的折线会把整张图缠住），
  // 越过最后一格记作「汇回课尾」。
  const forks = rawRoutes.map((route) => {
    const fork = int(route?.fork_node)
    return fork < 0 ? 0 : Math.min(fork, Math.max(0, count - 1))
  })
  const merges = rawRoutes.map((route, index) => {
    const fork = forks[index]
    const merge = int(route?.merge_node)
    if (merge <= fork) return Math.min(count, fork + 1)
    return Math.min(count, merge)
  })
  const terminal = merges.some((merge) => merge >= count)
  const lanes = assignLanes(forks.map((fork, index) => ({ fork, merge: merges[index] })))
  const laneCount = lanes.reduce((max, lane) => Math.max(max, lane), 0)
  const geometry = buildGeometry(count, laneCount, terminal)

  const branchNodes = new Set<number>()
  forks.forEach((fork) => branchNodes.add(fork))
  merges.forEach((merge) => branchNodes.add(merge))

  const nodes: NodeView[] = rawNodes.map((node, index) => {
    const page = int(node?.page)
    const n = nodeNo(node?.n, index)
    return {
      index,
      n,
      unitId: text(node?.unit_id),
      slide: int(node?.slide),
      page,
      pageTitle: text(node?.page_title),
      summary: text(node?.summary),
      preview: text(node?.preview),
      kp: toList<string>(node?.kp).map(text).filter(Boolean),
      estSec: num(node?.est_sec),
      pageEnd: node?.page_end === true,
      x: geometry.xs[index],
      hasBranch: branchNodes.has(index),
    }
  })

  const routes: RouteView[] = rawRoutes.map((route, index) => {
    const fork = forks[index]
    const merge = merges[index]
    const lane = lanes[index] || 1
    const wire = routeWire(geometry, fork, merge, lane)
    const kind = text(route?.kind) || 'skip'
    const label = text(route?.label)
    const span = Math.max(1, merge - fork)
    const isTerminal = merge >= count
    const head = label || routeKindLabel(kind)
    return {
      key: text(route?.route_id) || `route-${index}`,
      kind,
      kindLabel: routeKindLabel(kind),
      label,
      fork,
      merge,
      forkN: nodes[fork]?.n ?? fork,
      mergeN: isTerminal ? null : nodes[merge]?.n ?? merge,
      terminal: isTerminal,
      span,
      skipped: int(route?.skipped),
      repeats: int(route?.repeats),
      estSec: num(route?.est_sec),
      summary: text(route?.summary),
      reason: text(route?.reason),
      steps: toList(route?.steps).map((step) => ({
        unitId: text(step?.unit_id),
        label: text(step?.label),
        text: text(step?.text),
        estSec: num(step?.est_sec),
      })),
      lane,
      d: wire.d,
      chipX: wire.chipX,
      chipY: wire.chipY,
      chipText: `${head} · ${span} 格`,
    }
  })

  const totalsRaw = graph?.totals
  const pages = new Set(nodes.map((node) => node.page))
  const estTotal = nodes.reduce((sum, node) => sum + node.estSec, 0)
  return {
    width: geometry.width,
    height: geometry.height,
    mainY: geometry.mainY,
    cardTop: geometry.cardTop,
    mainLine: mainLinePath(geometry),
    nodes,
    routes,
    pages: pageBands(nodes.map((node) => ({ page: node.page, title: node.pageTitle })), geometry.xs),
    totals: {
      nodes: positive(totalsRaw?.nodes, nodes.length),
      routes: positive(totalsRaw?.routes, routes.length),
      slides: positive(totalsRaw?.slides, pages.size),
      est_sec: positive(totalsRaw?.est_sec, estTotal),
    },
  }
}
