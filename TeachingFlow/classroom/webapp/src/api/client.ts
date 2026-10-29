/**
 * §4 HTTP 契约的客户端封装。
 * 只用 fetch：后端没起来时统一抛 ApiError(status=0)，页面按「离线」处理。
 */
import type * as T from './types'

const V1 = '/api/v1'

export class ApiError extends Error {
  readonly status: number
  /** 错误体里的 code（如 no_provider / bad_request）：页面按码分派，不拿中文文案做子串匹配。 */
  readonly code: string

  constructor(message: string, status: number, code = '') {
    super(message)
    this.name = 'ApiError'
    this.status = status
    this.code = code
  }
}

function pickMessage(payload: unknown, depth = 0): string {
  if (depth > 2 || payload == null) return ''
  if (typeof payload === 'string') return payload.trim().slice(0, 300)
  if (typeof payload === 'object') {
    const record = payload as Record<string, unknown>
    for (const key of ['detail', 'message', 'error', 'msg']) {
      const value = record[key]
      if (typeof value === 'string' && value.trim()) return value.trim().slice(0, 300)
      if (value && typeof value === 'object') {
        const nested = pickMessage(value, depth + 1)
        if (nested) return nested
      }
    }
  }
  return ''
}

/** 只认顶层的 code：错误体是 {ok,message,code}，嵌套里的同名字段不是给页面分派用的。 */
function pickCode(payload: unknown): string {
  if (payload && typeof payload === 'object') {
    const code = (payload as Record<string, unknown>).code
    if (typeof code === 'string') return code.trim()
  }
  return ''
}

async function request<T>(url: string, init?: RequestInit): Promise<T> {
  let response: Response
  try {
    response = await fetch(url, init)
  } catch {
    throw new ApiError('连不上课堂服务：后端没起来，或端口/代理不对', 0)
  }
  if (response.status === 204) return undefined as T
  const raw = await response.text()
  let payload: unknown = null
  if (raw) {
    try {
      payload = JSON.parse(raw)
    } catch {
      payload = raw
    }
  }
  if (!response.ok) {
    const message = pickMessage(payload) || `请求失败（HTTP ${response.status}）`
    throw new ApiError(message, response.status, pickCode(payload))
  }
  return payload as T
}

function jsonBody(method: 'POST' | 'PUT' | 'PATCH' | 'DELETE', body?: unknown): RequestInit {
  return {
    method,
    headers: { 'Content-Type': 'application/json' },
    body: body === undefined ? undefined : JSON.stringify(body),
  }
}

export interface CreateSessionInput {
  unit_id: string
  profile_id: string
  /** 大纲页「从这里开始」：给了就从这一页开课（非法值后端会忽略）。 */
  start_slide?: number
}

export interface CreateGenerationInput {
  goal: string
  /** 学段必填：后端不对学段做猜测，缺了直接 400。 */
  stage: T.GenerationStage
  /** 学科可不填：填了就写进 --subject，没填后端用 --accept-detected 兜住。 */
  subject?: string
  term?: T.GenerationTerm
  level?: T.GenerationLevel
}

export interface ModelProviderInput {
  name: string
  kind: string
  base_url: string
  /** 新建必填；编辑时缺省＝不改动已存的 key，传 '' 才是清空。 */
  api_key?: string
  models?: string[]
  models_source?: T.ModelsSource | string
}

export const api = {
  health(): Promise<T.Health> {
    return request<T.Health>('/api/health')
  },

  getProfiles(): Promise<T.ProfilesResponse> {
    return request<T.ProfilesResponse>(V1 + '/profiles')
  },

  createProfile(name: string): Promise<T.Profile> {
    return request<T.Profile>(V1 + '/profiles', jsonBody('POST', { name }))
  },

  activateProfile(id: string): Promise<void> {
    return request<void>(V1 + `/profiles/${encodeURIComponent(id)}/activate`, jsonBody('POST', {}))
  },

  deleteProfile(id: string): Promise<void> {
    return request<void>(V1 + `/profiles/${encodeURIComponent(id)}`, jsonBody('DELETE'))
  },

  listCourses(profileId?: string): Promise<T.CourseSummary[]> {
    const query = profileId ? '?profile_id=' + encodeURIComponent(profileId) : ''
    return request<T.CourseSummary[]>(V1 + '/courses' + query)
  },

  scanCourses(): Promise<T.ScanResult> {
    return request<T.ScanResult>(V1 + '/courses/scan', jsonBody('POST', {}))
  },

  importCourse(file: File): Promise<T.ImportResult> {
    const form = new FormData()
    form.append('file', file)
    return request<T.ImportResult>(V1 + '/courses/import', { method: 'POST', body: form })
  },

  getCourse(id: string): Promise<T.CourseDetail> {
    return request<T.CourseDetail>(V1 + `/courses/${encodeURIComponent(id)}`)
  },

  deleteCourse(id: string): Promise<void> {
    return request<void>(V1 + `/courses/${encodeURIComponent(id)}`, jsonBody('DELETE'))
  },

  getUnit(id: string): Promise<T.UnitDetail> {
    return request<T.UnitDetail>(V1 + `/units/${encodeURIComponent(id)}`)
  },

  getUnitOutline(id: string): Promise<T.Outline> {
    return request<T.Outline>(V1 + `/units/${encodeURIComponent(id)}/outline`)
  },

  prepareUnit(id: string): Promise<T.PrepareResult> {
    return request<T.PrepareResult>(V1 + `/units/${encodeURIComponent(id)}/prepare`, jsonBody('POST', {}))
  },

  createSession(input: CreateSessionInput): Promise<T.SessionCreated> {
    const body: Record<string, unknown> = { unit_id: input.unit_id, profile_id: input.profile_id }
    if (typeof input.start_slide === 'number') body.start_slide = input.start_slide
    return request<T.SessionCreated>(V1 + '/sessions', jsonBody('POST', body))
  },

  getSession(id: string): Promise<T.SessionDetail> {
    return request<T.SessionDetail>(V1 + `/sessions/${encodeURIComponent(id)}`)
  },

  endSession(id: string): Promise<T.EndSessionResult> {
    return request<T.EndSessionResult>(V1 + `/sessions/${encodeURIComponent(id)}/end`, jsonBody('POST', {}))
  },

  getSessionRecap(id: string): Promise<T.Recap> {
    return request<T.Recap>(V1 + `/sessions/${encodeURIComponent(id)}/recap`)
  },

  getGenerationConfig(): Promise<T.GenerationConfig> {
    return request<T.GenerationConfig>(V1 + '/generations/config')
  },

  createGeneration(input: CreateGenerationInput): Promise<T.GenerationSnapshot> {
    const body: Record<string, unknown> = { goal: input.goal, stage: input.stage }
    if (input.subject) body.subject = input.subject
    if (input.term) body.term = input.term
    if (input.level) body.level = input.level
    return request<T.GenerationSnapshot>(V1 + '/generations', jsonBody('POST', body))
  },

  getGeneration(id: string): Promise<T.GenerationSnapshot> {
    return request<T.GenerationSnapshot>(V1 + '/generations/' + encodeURIComponent(id))
  },

  listGenerations(limit = 10): Promise<T.GenerationList> {
    return request<T.GenerationList>(V1 + '/generations?limit=' + encodeURIComponent(String(limit)))
  },

  cancelGeneration(id: string): Promise<T.GenerationSnapshot> {
    return request<T.GenerationSnapshot>(V1 + '/generations/' + encodeURIComponent(id) + '/cancel', jsonBody('POST', {}))
  },

  // 设置 · 模型服务商：出片页只读 /model-providers 的 default，写操作全在这一组。
  listModelProviders(): Promise<T.ModelProviderList> {
    return request<T.ModelProviderList>(V1 + '/model-providers')
  },

  createModelProvider(input: ModelProviderInput): Promise<T.ModelProvider> {
    return request<T.ModelProvider>(V1 + '/model-providers', jsonBody('POST', input))
  },

  updateModelProvider(id: string, input: ModelProviderInput): Promise<T.ModelProvider> {
    return request<T.ModelProvider>(V1 + '/model-providers/' + encodeURIComponent(id), jsonBody('PATCH', input))
  },

  deleteModelProvider(id: string): Promise<void> {
    return request<void>(V1 + '/model-providers/' + encodeURIComponent(id), jsonBody('DELETE'))
  },

  /** 让后端带着已存的 key 去问服务商要模型清单；502 的 message 里有 base_url 和原因。 */
  fetchModelProviderModels(id: string): Promise<T.ModelFetchResult> {
    return request<T.ModelFetchResult>(V1 + '/model-providers/' + encodeURIComponent(id) + '/models', jsonBody('POST', {}))
  },

  /** 真发一次对话：拉得到清单不等于出得了片，余额不足只有对话才暴露。结论在 200 body 里，ok=false 也是结论。 */
  probeModelProvider(id: string, model?: string): Promise<T.ModelProbeResult> {
    const body: Record<string, unknown> = {}
    if (model) body.model = model
    return request<T.ModelProbeResult>(V1 + '/model-providers/' + encodeURIComponent(id) + '/probe', jsonBody('POST', body))
  },

  saveModelProviderModels(id: string, models: string[]): Promise<T.ModelProvider> {
    return request<T.ModelProvider>(V1 + '/model-providers/' + encodeURIComponent(id) + '/models', jsonBody('PUT', { models }))
  },

  setModelDefault(providerId: string, model: string): Promise<T.ModelDefaultResult> {
    return request<T.ModelDefaultResult>(V1 + '/model-default', jsonBody('POST', { provider_id: providerId, model }))
  },

  exportModelEnv(): Promise<T.ModelEnvExport> {
    return request<T.ModelEnvExport>(V1 + '/model-providers/export-env', jsonBody('POST', {}))
  },
}
