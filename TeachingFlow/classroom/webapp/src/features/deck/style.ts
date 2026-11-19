import type { CSSProperties } from 'vue'
import type { DeckFill, DeckLine, DeckPara, DeckRun } from './types'

/** 0.35 -> '59'：把 alpha 拼进 #rrggbb。 */
function hexAlpha(alpha: number): string {
  return Math.max(0, Math.min(255, Math.round(alpha * 255)))
    .toString(16)
    .padStart(2, '0')
}

export function cssColor(color: string | undefined, alpha?: number): string {
  const base = color || '#000'
  return alpha != null && alpha < 1 ? base + hexAlpha(alpha) : base
}

export function fillCss(fill: DeckFill | null | undefined): string {
  if (!fill || fill.kind === 'none') return 'transparent'
  if (fill.kind === 'solid') return cssColor(fill.color, fill.alpha)
  if (fill.kind === 'gradient' && fill.gradient && fill.gradient.length) {
    const stops = fill.gradient
      .map((stop) => `${cssColor(stop.color, stop.alpha)} ${Math.round((stop.pos || 0) * 100)}%`)
      .join(',')
    // 解析器已把 DrawingML 角度换算成 CSS 约定（0° 向上）
    return `linear-gradient(${(fill.angle || 0).toFixed(2)}deg, ${stops})`
  }
  if (fill.kind === 'image') return '#eef1f6'
  return 'transparent'
}

/** 渐变/图片描边 CSS 表达不了：宁可不画，也不要画成黑边。 */
export function lineCss(line: DeckLine | null | undefined): string | undefined {
  if (!line || line.kind === 'none' || !line.width_px) return undefined
  if (line.kind !== 'solid') return undefined
  const dash = (line.dash || '').toLowerCase()
  // prstDash 有 dash / lgDash / sysDash / dashDot…，custDash 是自定义虚线；
  // CSS 只能表达三种，凡是「虚」的统一画虚线（以前 lgDash 落到 solid，虚线框变实线）。
  const style = !dash
    ? 'solid'
    : dash.includes('dot')
      ? 'dotted'
      : dash.includes('dash') || dash === 'sys' || dash === 'custom'
        ? 'dashed'
        : 'solid'
  return `${line.width_px.toFixed(1)}px ${style} ${cssColor(line.color, line.alpha)}`
}

export function fontStack(font?: string | null, fontEa?: string | null): string {
  const families: string[] = []
  if (font) families.push(`"${font}"`)
  if (fontEa && fontEa !== font) families.push(`"${fontEa}"`)
  // 课件指定了本机没装的字体时（例如生成管线用的 MiSans），PowerPoint 会做字体替换；
  // 浏览器只按这个栈往下找。Noto Sans SC 与 MiSans/思源黑体同属现代无衬线，实测
  // 比直接落到微软雅黑更接近 PowerPoint 的替换结果；雅黑仍是下一档兜底。
  families.push(
    '"Noto Sans SC"',
    '"Source Han Sans SC"',
    '"Microsoft YaHei"',
    '"PingFang SC"',
    'system-ui',
    'sans-serif',
  )
  return families.join(',')
}

const ptToPx = (pt: number, scale: number): string => ((pt * scale * 4) / 3).toFixed(2) + 'px'

/**
 * PowerPoint 的「单倍行距」。
 *
 * 浏览器 `line-height: normal` 用的是字体自己的 hhea 指标：SimSun 在 Chrome 里
 * 是 1.667em，而 PowerPoint 对同一份 18pt 文本的行距是 1.5em（实测一份真实课件：
 * 三行 18pt 文本的基线间距 36px = 24px × 1.5，形状高度 109px 正好是 3 × 36）。
 * 不写死这个值，每行就多出 4px，第三行会被文本框裁掉 —— 看起来就是「字少了一截」。
 */
const LINE_HEIGHT = 1.5

/** 这一段的实际字号：取 run 里最大的那个；没有 run 字号才退回 defRPr。 */
export function paraSizePt(para: DeckPara): number | null {
  const sizes = (para.runs ?? [])
    .map((run) => run.size_pt)
    .filter((value): value is number => typeof value === 'number' && value > 0)
  if (sizes.length) return Math.max(...sizes)
  return para.default_size_pt ?? null
}

export function runStyle(run: DeckRun, scale: number): CSSProperties {
  const style: CSSProperties = { fontFamily: fontStack(run.font, run.font_ea) }
  if (run.size_pt != null) style.fontSize = ptToPx(run.size_pt, scale)
  if (run.color) style.color = run.color
  if (run.bold) style.fontWeight = 700
  if (run.italic) style.fontStyle = 'italic'
  const decoration: string[] = []
  if (run.underline) decoration.push('underline')
  if (run.strike) decoration.push('line-through')
  if (decoration.length) style.textDecoration = decoration.join(' ')
  // DrawingML 的 spc 是 1/100 pt，解析器已折成 pt；不渲染它，字距就和 PowerPoint 对不上。
  if (run.spacing_pt) style.letterSpacing = ptToPx(run.spacing_pt, scale)
  if (run.baseline === 'superscript') {
    style.verticalAlign = 'super'
    style.fontSize = '0.75em'
  }
  if (run.baseline === 'subscript') {
    style.verticalAlign = 'sub'
    style.fontSize = '0.75em'
  }
  return style
}

export function paraStyle(para: DeckPara, scale: number): CSSProperties {
  const style: CSSProperties = {}
  if (para.align) style.textAlign = para.align as CSSProperties['textAlign']
  // 课件里写了 spcPct 就用它；没写时用 PowerPoint 的单倍行距，而不是浏览器 normal。
  style.lineHeight = para.line_spacing ?? LINE_HEIGHT
  if (para.space_before_pt) style.marginTop = ptToPx(para.space_before_pt, scale)
  if (para.space_after_pt) style.marginBottom = ptToPx(para.space_after_pt, scale)
  const marginLeft = para.margin_left_px || 0
  const firstLine = para.first_line_px || 0
  if (marginLeft || firstLine) {
    style.paddingLeft = (marginLeft * scale).toFixed(2) + 'px'
    style.textIndent = (firstLine * scale).toFixed(2) + 'px'
  } else if (para.indent_px) {
    style.textIndent = (para.indent_px * scale).toFixed(2) + 'px'
  }
  return style
}

/** 段落级默认样式打在容器上，run 级样式覆盖它（真实课件常把字号写在 defRPr）。
 *
 * `sizePt` 由调用方按 `paraSizePt()` 算好传进来：容器的 font-size 会撑起行盒，
 * 拿 defRPr 的 18pt 去撑一个只有 13.5pt run 的段落，行盒就比文本框高，字会被裁。
 */
export function paraBaseStyle(para: DeckPara, scale: number, sizePt?: number | null): CSSProperties {
  const style: CSSProperties = {}
  const size = sizePt ?? para.default_size_pt
  if (size != null) style.fontSize = ptToPx(size, scale)
  if (para.default_color) style.color = para.default_color
  if (para.default_font) style.fontFamily = fontStack(para.default_font)
  if (para.default_bold) style.fontWeight = 700
  return style
}

export const PRESET_CLIP: Record<string, string> = {
  chevron: 'polygon(0 0, 86% 0, 100% 50%, 86% 100%, 0 100%, 14% 50%)',
  homePlate: 'polygon(0 0, 78% 0, 100% 50%, 78% 100%, 0 100%)',
  rightArrow: 'polygon(0 25%, 64% 25%, 64% 0, 100% 50%, 64% 100%, 64% 75%, 0 75%)',
  leftArrow: 'polygon(36% 25%, 100% 25%, 100% 75%, 36% 75%, 36% 100%, 0 50%, 36% 0)',
  triangle: 'polygon(50% 0, 100% 100%, 0 100%)',
  isocelesTriangle: 'polygon(50% 0, 100% 100%, 0 100%)',
  rtTriangle: 'polygon(0 0, 100% 100%, 0 100%)',
  diamond: 'polygon(50% 0, 100% 50%, 50% 100%, 0 50%)',
  parallelogram: 'polygon(22% 0, 100% 0, 78% 100%, 0 100%)',
  trapezoid: 'polygon(22% 0, 78% 0, 100% 100%, 0 100%)',
  pentagon: 'polygon(50% 0, 100% 38%, 81% 100%, 19% 100%, 0 38%)',
  hexagon: 'polygon(25% 0, 75% 0, 100% 50%, 75% 100%, 25% 100%, 0 50%)',
  star5: 'polygon(50% 0, 61% 35%, 98% 35%, 68% 57%, 79% 91%, 50% 70%, 21% 91%, 32% 57%, 2% 35%, 39% 35%)',
  plus: 'polygon(35% 0, 65% 0, 65% 35%, 100% 35%, 100% 65%, 65% 65%, 65% 100%, 35% 100%, 35% 65%, 0 65%, 0 35%, 35% 35%)',
  donut: 'none',
  ellipse: 'none',
}

export function roundedStyle(kind: string, preset: string | undefined, w: number, h: number): CSSProperties {
  if (kind === 'ellipse' || preset === 'ellipse' || preset === 'donut') {
    return { borderRadius: `${w / 2}px / ${h / 2}px` }
  }
  if (preset === 'roundRect') {
    // 注意：Vue 的内联样式不像 React 那样给数字补 px，这里必须显式带单位。
    return { borderRadius: `${Math.min(18, Math.min(w, h) * 0.14)}px` }
  }
  return {}
}

export function clipFor(preset: string | undefined): string | undefined {
  if (!preset) return undefined
  const clip = PRESET_CLIP[preset]
  return clip && clip !== 'none' ? clip : undefined
}

export function resolveImageSrc(src: string | undefined, externalUrl: string | undefined, mediaBase: string): string {
  if (externalUrl && /^(https?:|data:)/i.test(externalUrl)) return externalUrl
  if (!src) return ''
  if (/^(https?:|data:)/i.test(src)) return src
  const base = mediaBase ? (mediaBase.endsWith('/') ? mediaBase : mediaBase + '/') : ''
  return base + src.replace(/^media\//, '')
}
