/**
 * 课堂走向图的几何：一组纯函数，只吃「有几格、路线从哪到哪」，吐 x / y。
 *
 * 为什么单独抽成纯函数：实时指针每 5 秒刷一次，卡片位置必须一个字都不变。
 * 位置只在数据到手时算一次，之后轮询只改 class 与文字，整张图不会抖；
 * 低缩放「退化成色块」也只改 CSS，不改任何 left/top。
 */

/** 主线卡片：默认 120×52，相邻两张之间留 140 的空档（折线与 chip 就画在这里）。 */
export const CARD_W = 120
export const CARD_H = 52
export const CARD_GAP = 140
/** 相邻两格的中心距。 */
export const STEP = CARD_W + CARD_GAP
/** 画布左右留白：第一格与最后一格不贴边。 */
export const PAD_X = 48
/** 顶部页标尺条高。 */
export const PAGE_RULER_H = 30
/** 指针气泡占的一条带（在主线之上；气泡本体画在屏幕层，不随缩放变小）。 */
export const BUBBLE_BAND = 36
/** 一条泳道的行高：必须大于 (卡片高 + chip 高) / 2，否则下层 chip 会压住主线卡片。 */
export const LANE_H = 46
/** 进出泳道的横向偏移：等于 LANE_H 时折角正好 45°。 */
export const KNEE = 46
/** 有「汇回课尾」的路线时右侧多留的宽度（放课尾标记，否则会被裁掉）。 */
export const TAIL = 110
/** 低于这个缩放（35%）卡片退化成色块：再小也读不出字，留着文字只是一团灰。 */
export const COMPACT_K = 0.35
/** 缩放下限：177 格的课在 900px 视窗里「适应全课」大约要 0.02，这里留足余量。 */
export const MIN_K = 0.005
export const MAX_K = 2

export interface Geometry {
  count: number
  /** 每一格卡片的左边缘（画布坐标）。 */
  xs: number[]
  width: number
  height: number
  /** 主线（卡片水平中心）的 y。 */
  mainY: number
  cardTop: number
  laneCount: number
}

export function buildGeometry(count: number, laneCount: number, terminal: boolean): Geometry {
  const total = Math.max(0, count)
  const xs: number[] = []
  for (let i = 0; i < total; i += 1) xs.push(PAD_X + i * STEP)
  const lastRight = total > 0 ? xs[total - 1] + CARD_W : PAD_X + CARD_W
  const width = lastRight + PAD_X + (terminal ? TAIL : 0)
  // 泳道往主线两侧交替叠：1 上、2 下、3 上二层……所以两层泳道要占两行的高度。
  const rows = Math.max(1, Math.ceil(Math.max(0, laneCount) / 2))
  const bodyH = rows * 2 * LANE_H + CARD_H + 24
  const bodyTop = BUBBLE_BAND + PAGE_RULER_H
  const mainY = bodyTop + bodyH / 2
  return { count: total, xs, width, height: bodyTop + bodyH + 16, mainY, cardTop: mainY - CARD_H / 2, laneCount }
}

/** lane 1 在主线之上、2 在其下、3 在二层之上：剧情图的泳道惯例，读起来是「两边的岔口」。 */
export function laneY(geometry: Geometry, lane: number): number {
  const row = Math.max(1, Math.ceil(lane / 2))
  return geometry.mainY + (lane % 2 === 1 ? -row : row) * LANE_H
}

export interface LaneInput {
  fork: number
  merge: number
}

/**
 * 泳道分配：同一条泳道上只放横向区间不重叠的路线。
 * 按岔出格从左到右贪心入座，同起点时跨度大的先挑靠近主线的泳道 ——
 * 这样短路线（最常见）永远贴着主线，长路线被挤到外层，图不会一上来就臃肿。
 */
export function assignLanes(items: LaneInput[]): number[] {
  const lanes: number[][] = []
  const result: number[] = items.map(() => 1)
  const order = items
    .map((_, index) => index)
    .sort((a, b) => {
      const byFork = items[a].fork - items[b].fork
      return byFork !== 0 ? byFork : items[b].merge - items[b].fork - (items[a].merge - items[a].fork)
    })
  order.forEach((index) => {
    const { fork, merge } = items[index]
    let lane = 1
    for (;;) {
      const occupied = lanes[lane] || []
      let free = true
      for (let i = 0; i < occupied.length; i += 2) {
        if (fork <= occupied[i + 1] && occupied[i] <= merge) {
          free = false
          break
        }
      }
      if (free) break
      lane += 1
    }
    if (!lanes[lane]) lanes[lane] = []
    lanes[lane].push(fork, merge)
    result[index] = lane
  })
  return result
}

export interface RouteWire {
  /** SVG path：卡片右缘 → 45° 折进泳道 → 沿泳道 → 45° 折回下一格左缘。 */
  d: string
  chipX: number
  chipY: number
  lane: number
  /** 汇回点越过最后一格：路线画到课尾，不回主线。 */
  terminal: boolean
}

/**
 * 一条路线的折线。merge 已经归一化到 [fork + 1, count]（count = 汇回课尾），
 * 所以这里只负责把「折角 45°」这件事算准：横向偏移 kx 取 min(|dy|, KNEE)，
 * 第一层泳道的 |dy| 正好等于 KNEE —— 于是所有路线都是从同一个角度岔出去的。
 */
export function routeWire(geometry: Geometry, fork: number, merge: number, lane: number): RouteWire {
  const y0 = geometry.mainY
  const y1 = laneY(geometry, lane)
  const xr = (geometry.xs[fork] || 0) + CARD_W
  const terminal = merge >= geometry.count
  const xm = terminal ? (geometry.xs[geometry.count - 1] || 0) + CARD_W + 30 : geometry.xs[merge] || 0
  const kx = Math.min(Math.abs(y1 - y0), KNEE)
  const xa = xr + kx
  // 间隙不够时退化成竖折：绝不往回画，往回画的折线会把整张图缠住。
  const xb = Math.max(xa, terminal ? xm : xm - kx)
  const d = terminal
    ? `M${xr} ${y0}L${xa} ${y1}L${xb} ${y1}`
    : `M${xr} ${y0}L${xa} ${y1}L${xb} ${y1}L${xm} ${y0}`
  return { d, chipX: (xa + xb) / 2, chipY: y1, lane, terminal }
}

export interface PageBand {
  key: string
  page: number
  title: string
  left: number
  width: number
  /** 隔页交替底色：缩到很小时页与页的分界靠这条带子还读得出来。 */
  alt: boolean
}

/** 页标尺：相邻两页的分界取在两张卡片正中，线才不会压着某一格。 */
export function pageBands(pages: { page: number; title: string }[], xs: number[]): PageBand[] {
  const bands: PageBand[] = []
  if (!pages.length || !xs.length) return bands
  const boundary = (firstOfNext: number) => (xs[firstOfNext] || 0) - CARD_GAP / 2
  let start = 0
  for (let i = 1; i <= pages.length; i += 1) {
    if (i !== pages.length && pages[i].page === pages[start].page) continue
    const left = start === 0 ? xs[0] - 22 : boundary(start)
    const right = i === pages.length ? (xs[i - 1] || 0) + CARD_W + 22 : boundary(i)
    bands.push({
      key: `page-${start}-${i}`,
      page: pages[start].page,
      title: pages[start].title,
      left,
      width: Math.max(2, right - left),
      alt: bands.length % 2 === 1,
    })
    start = i
  }
  return bands
}

/** 把整排卡片串起来的那条横线（卡片不透明，线只从空档里露出来）。 */
export function mainLinePath(geometry: Geometry): string {
  if (geometry.count === 0) return ''
  const y = geometry.mainY
  return `M${geometry.xs[0]} ${y}L${geometry.xs[geometry.count - 1] + CARD_W} ${y}`
}

/** 平移夹紧：内容比视口小就居中，比视口大就夹在两端 —— 既拖不出视野，也能真的拖到第一格和最后一格。 */
export function clamp(value: number, min: number, max: number): number {
  return Math.min(max, Math.max(min, value))
}
