/**
 * §4 HTTP 契约 v1 的类型化定义（唯一事实源：docs/正式版设计.md）。
 * 前端只依赖这份契约，不读后端源码。
 */

export type CourseSource = 'run' | 'import'
export type UnitStatus = 'not_started' | 'in_progress' | 'completed' | 'paused'

export interface Profile {
  id: string
  name: string
  created_at: string
}

export interface ProfilesResponse {
  active_id: string
  items: Profile[]
}

export interface CourseProgress {
  started: number
  completed: number
  percent: number
}

export interface CourseSummary {
  id: string
  title: string
  subject: string
  source: CourseSource | string
  unit_count: number
  unit_ready_count: number
  progress: CourseProgress
  updated_at: string
}

export interface ScanResult {
  added: number
  updated: number
  total: number
  warnings?: string[]
}

export interface ImportResult {
  course_id: string
  unit_id: string
  slides: number
  warnings?: string[]
}

export interface UnitProgress {
  percent: number
  last_slide: number
  status: UnitStatus | string
}

export interface CourseUnit {
  id: string
  title: string
  slides: number
  est_sec: number
  ready: boolean
  plan_source: string
  progress: UnitProgress
}

export interface CourseChapter {
  title: string
  units: CourseUnit[]
}

export interface CourseDetail {
  id: string
  title: string
  subject: string
  source: CourseSource | string
  chapters: CourseChapter[]
}

export interface UnitDetail {
  id: string
  course_id: string
  title: string
  slides: number
  est_sec: number
  ready: boolean
  plan_source: string
  progress: UnitProgress
}

export interface OutlineUnit {
  preview: string
  est_sec: number
  kp: string[]
}

export interface OutlinePage {
  index: number
  title: string
  units: OutlineUnit[]
}

export interface Outline {
  totals: { slides: number; units: number; est_sec: number }
  pages: OutlinePage[]
}

export interface PrepareResult {
  ok: boolean
  deck_ready: boolean
  plan_ready: boolean
  plan_source: string
  degraded: boolean
  warnings: string[]
  slides?: number
  est_sec?: number
}

export interface SessionPosition {
  slide: number
  unit_index: number
  from_progress?: boolean
}

export interface SessionCreated {
  session_id: string
  resumed: boolean
  position: SessionPosition
}

export interface SessionDetail {
  id: string
  unit_id: string
  status: 'active' | 'paused' | 'ended' | string
  started_at: string
  elapsed_sec: number
  position: SessionPosition
  comprehension: number | null
  qa_count: number
}

export interface Recap {
  elapsed_sec: number
  slides_covered: number
  units_given: number
  qa_count: number
  topics: string[]
}

export interface EndSessionResult {
  ok: boolean
  recap: Recap
}

export interface Health {
  ok: boolean
  version: string
  courses: number
  engine: { provider: string; model: string; tts: string | boolean }
}

/** 出片任务（§4 generations 契约）：页面只读快照，不自己拼命令。 */
export type GenerationStatus = 'queued' | 'running' | 'succeeded' | 'failed' | 'cancelled' | 'interrupted'
export type GenerationMode = 'real' | 'mock'
export type GenerationTerm = 'long' | 'short'
export type GenerationLevel = 'beginner' | 'intermediate'
export type GenerationStage = '小学' | '初中' | '高中' | '大学' | '考研'

export interface GenerationConfig {
  ready: boolean
  has_key: boolean
  model: string
  base_url: string
  mock_allowed: boolean
  mode: GenerationMode | string
  reason: string
}

export interface GenerationProgress {
  done: number
  total: number
  percent: number
}

export interface GenerationScan {
  added: number
  updated: number
  total: number
}

export interface GenerationResult {
  course_id: string
  course_title: string
  pptx: string[]
  scan: GenerationScan
}

export interface GenerationSnapshot {
  id: string
  goal: string
  status: GenerationStatus | string
  mode: GenerationMode | string
  stage: string
  stage_label: string
  progress: GenerationProgress
  /** 第一条是后端真正执行的命令行原文，页面「高级」区要原样展示。 */
  log_tail: string[]
  started_at: string | null
  finished_at: string | null
  elapsed_sec: number
  result: GenerationResult | null
  error: string
}

export interface GenerationList {
  items: GenerationSnapshot[]
  active: GenerationSnapshot | null
}

/** §4 model-providers 契约：页面只拿得到掩码 key，明文 key 只在提交时上送。 */
export type ModelProviderKind = 'deepseek' | 'openai' | 'ollama' | 'openrouter' | 'custom'
export type ModelsSource = 'provider' | 'manual' | ''

export interface ModelProvider {
  id: string
  name: string
  kind: string
  base_url: string
  api_key_masked: string
  has_key: boolean
  models: string[]
  models_source: ModelsSource | string
  created_at: string
  updated_at: string
}

export interface ModelDefault {
  provider_id: string
  model: string
  provider: ModelProvider
}

export interface ModelProviderList {
  items: ModelProvider[]
  default: ModelDefault | null
}

export interface ModelFetchResult {
  models: string[]
  models_source: ModelsSource | string
  fetched_at: string
}

/**
 * POST /model-providers/{id}/probe：真发一次对话。
 * 拉模型清单只要有 key 就 200，余额不足只有真发一次对话才暴露，所以这里问的是「能不能对话」。
 * 探测结论一律 200 返回：ok=false 是结论不是请求错误；status=0 表示连不上服务商/超时。
 */
export interface ModelProbeResult {
  ok: boolean
  status: number
  message: string
  hint: string
}

export interface ModelDefaultResult extends ModelDefault {
  /** 后端提醒（例如这个模型不在拉取到的清单里）：有内容就必须让用户看见。 */
  warning: string
}

export interface ModelEnvExport {
  /** .env 的绝对路径：用户要知道到底写进了哪一个文件。 */
  path: string
  written: string[]
  skipped: string[]
}
