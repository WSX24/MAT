/**
 * §4 HTTP 契约的客户端封装。
 * 只用 fetch —— 后端没就绪时把 VITE_MOCK=1 打开，接口由内置假数据接管。
 */
import type * as T from './types';
import { isMock, mockApi } from './mock';

const V1 = '/api/v1';

export class ApiError extends Error {
  readonly status: number;

  constructor(message: string, status: number) {
    super(message);
    this.name = 'ApiError';
    this.status = status;
  }
}

function pickMessage(payload: unknown, depth = 0): string {
  if (depth > 2 || payload == null) return '';
  if (typeof payload === 'string') return payload.trim().slice(0, 300);
  if (typeof payload === 'object') {
    const record = payload as Record<string, unknown>;
    for (const key of ['detail', 'message', 'error', 'msg']) {
      const value = record[key];
      if (typeof value === 'string' && value.trim()) return value.trim().slice(0, 300);
      if (value && typeof value === 'object') {
        const nested = pickMessage(value, depth + 1);
        if (nested) return nested;
      }
    }
  }
  return '';
}

async function request<T>(url: string, init?: RequestInit): Promise<T> {
  let response: Response;
  try {
    response = await fetch(url, init);
  } catch {
    throw new ApiError('连不上课堂服务：后端没起来，或端口/代理不对', 0);
  }
  if (response.status === 204) return undefined as T;
  const raw = await response.text();
  let payload: unknown = null;
  if (raw) {
    try {
      payload = JSON.parse(raw);
    } catch {
      payload = raw;
    }
  }
  if (!response.ok) {
    const message = pickMessage(payload) || `请求失败（HTTP ${response.status}）`;
    throw new ApiError(message, response.status);
  }
  return payload as T;
}

function jsonBody(method: 'POST' | 'PUT' | 'DELETE', body?: unknown): RequestInit {
  return {
    method,
    headers: { 'Content-Type': 'application/json' },
    body: body === undefined ? undefined : JSON.stringify(body),
  };
}

export interface CreateSessionInput {
  unit_id: string;
  profile_id: string;
  /** 非契约字段：大纲页"从这里开始"带起始页，后端支持时生效（见交付说明）。 */
  start_slide?: number;
}

export const api = {
  health(): Promise<T.Health> {
    if (isMock) return mockApi.health();
    return request<T.Health>('/api/health');
  },

  getProfiles(): Promise<T.ProfilesResponse> {
    if (isMock) return mockApi.getProfiles();
    return request<T.ProfilesResponse>(V1 + '/profiles');
  },

  createProfile(name: string): Promise<T.Profile> {
    if (isMock) return mockApi.createProfile(name);
    return request<T.Profile>(V1 + '/profiles', jsonBody('POST', { name }));
  },

  activateProfile(id: string): Promise<void> {
    if (isMock) return mockApi.activateProfile(id);
    return request<void>(V1 + `/profiles/${encodeURIComponent(id)}/activate`, jsonBody('POST', {}));
  },

  deleteProfile(id: string): Promise<void> {
    if (isMock) return mockApi.deleteProfile(id);
    return request<void>(V1 + `/profiles/${encodeURIComponent(id)}`, jsonBody('DELETE'));
  },

  listCourses(profileId?: string): Promise<T.CourseSummary[]> {
    const query = profileId ? '?profile_id=' + encodeURIComponent(profileId) : '';
    if (isMock) return mockApi.listCourses();
    return request<T.CourseSummary[]>(V1 + '/courses' + query);
  },

  scanCourses(): Promise<T.ScanResult> {
    if (isMock) return mockApi.scanCourses();
    return request<T.ScanResult>(V1 + '/courses/scan', jsonBody('POST', {}));
  },

  importCourse(file: File): Promise<T.ImportResult> {
    if (isMock) return mockApi.importCourse(file);
    const form = new FormData();
    form.append('file', file);
    return request<T.ImportResult>(V1 + '/courses/import', { method: 'POST', body: form });
  },

  getCourse(id: string): Promise<T.CourseDetail> {
    if (isMock) return mockApi.getCourse(id);
    return request<T.CourseDetail>(V1 + `/courses/${encodeURIComponent(id)}`);
  },

  deleteCourse(id: string): Promise<void> {
    if (isMock) return mockApi.deleteCourse(id);
    return request<void>(V1 + `/courses/${encodeURIComponent(id)}`, jsonBody('DELETE'));
  },

  getUnit(id: string): Promise<T.UnitDetail> {
    if (isMock) return mockApi.getUnit(id);
    return request<T.UnitDetail>(V1 + `/units/${encodeURIComponent(id)}`);
  },

  getUnitOutline(id: string): Promise<T.Outline> {
    if (isMock) return mockApi.getUnitOutline(id);
    return request<T.Outline>(V1 + `/units/${encodeURIComponent(id)}/outline`);
  },

  prepareUnit(id: string): Promise<T.PrepareResult> {
    if (isMock) return mockApi.prepareUnit(id);
    return request<T.PrepareResult>(V1 + `/units/${encodeURIComponent(id)}/prepare`, jsonBody('POST', {}));
  },

  createSession(input: CreateSessionInput): Promise<T.SessionCreated> {
    if (isMock) return mockApi.createSession(input);
    const body: Record<string, unknown> = { unit_id: input.unit_id, profile_id: input.profile_id };
    if (typeof input.start_slide === 'number') body.start_slide = input.start_slide;
    return request<T.SessionCreated>(V1 + '/sessions', jsonBody('POST', body));
  },

  getSession(id: string): Promise<T.SessionDetail> {
    if (isMock) return mockApi.getSession(id);
    return request<T.SessionDetail>(V1 + `/sessions/${encodeURIComponent(id)}`);
  },

  endSession(id: string): Promise<T.EndSessionResult> {
    if (isMock) return mockApi.endSession(id);
    return request<T.EndSessionResult>(V1 + `/sessions/${encodeURIComponent(id)}/end`, jsonBody('POST', {}));
  },

  getSessionRecap(id: string): Promise<T.Recap> {
    if (isMock) return mockApi.getSessionRecap(id);
    return request<T.Recap>(V1 + `/sessions/${encodeURIComponent(id)}/recap`);
  },
};

/** UI 需要判断是不是 mock（显示一个角标，避免把假数据当真）。 */
export const mockMode = isMock;
