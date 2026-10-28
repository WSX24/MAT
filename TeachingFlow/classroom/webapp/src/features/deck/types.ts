/**
 * 课件数据结构（与 deck/schema.py 的 JSON 对齐）。
 * 渲染只照数据画：不解析 pptx、不猜缺失字段。
 */

export interface DeckGradientStop {
  color: string
  alpha?: number
  pos?: number
}

export interface DeckFill {
  kind: string
  color?: string
  alpha?: number
  gradient?: DeckGradientStop[]
  angle?: number
  image?: string
}

export interface DeckLine {
  kind: string
  color?: string
  alpha?: number
  width_px?: number
  dash?: string
  cap?: string
}

export interface DeckRun {
  text?: string
  bold?: boolean | null
  italic?: boolean | null
  underline?: boolean | null
  strike?: boolean | null
  size_pt?: number | null
  color?: string | null
  font?: string | null
  font_ea?: string | null
  baseline?: string
  spacing_pt?: number | null
}

export interface DeckPara {
  pid?: string
  text?: string
  runs?: DeckRun[]
  level?: number
  align?: string
  bullet?: string | null
  bullet_char?: string
  space_before_pt?: number | null
  space_after_pt?: number | null
  line_spacing?: number | null
  indent_px?: number
  margin_left_px?: number
  first_line_px?: number
  default_size_pt?: number | null
  default_color?: string | null
  default_font?: string | null
  default_bold?: boolean | null
}

export interface DeckText {
  paras?: DeckPara[]
  anchor?: string
  wrap?: boolean
  autofit?: string
  font_scale?: number
  insets_px?: number[]
  vertical?: string
}

export interface DeckPic {
  src?: string
  external_url?: string
  natural_w?: number
  natural_h?: number
  crop?: number[]
  alt?: string
}

export interface DeckCell {
  text?: string
  pid?: string
  paras?: DeckPara[]
  fill?: DeckFill | null
  row_span?: number
  col_span?: number
}

export interface DeckTable {
  col_w_px?: number[]
  row_h_px?: number[]
  cells?: DeckCell[][]
}

export type DeckBox = [number, number, number, number]

export interface DeckElement {
  element_id: string
  kind: string
  role?: string
  box: DeckBox
  rot?: number
  flip_h?: boolean
  flip_v?: boolean
  z?: number
  fill?: DeckFill | null
  line?: DeckLine | null
  text?: DeckText | null
  pic?: DeckPic | null
  table?: DeckTable | null
  svg_path?: string
  preset?: string
  hidden?: boolean
  name?: string
  source?: string
  decorative?: boolean
}

export interface DeckSlide {
  index: number
  title?: string
  background?: DeckFill | null
  elements?: DeckElement[]
  decorations?: DeckElement[]
  width?: number
  height?: number
  width_px?: number
  height_px?: number
  media_base?: string
}
