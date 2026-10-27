/**
 * VITE_MOCK=1 的内置假数据。
 * 场景：?mock=empty | error | slow | unprepared（首次读取后写进 sessionStorage，
 * 之后的页面跳转仍然生效）。默认 default 为完整数据。
 */
import type * as T from './types';

export type MockScenario = 'default' | 'empty' | 'error' | 'slow' | 'unprepared';

const SCENARIO_KEY = 'classroom.mockScenario';
const SCENARIOS: MockScenario[] = ['default', 'empty', 'error', 'slow', 'unprepared'];

export const isMock = import.meta.env.VITE_MOCK === '1';

export function mockScenario(): MockScenario {
  try {
    const fromQuery = new URLSearchParams(window.location.search).get('mock');
    if (fromQuery) sessionStorage.setItem(SCENARIO_KEY, fromQuery);
    const raw = fromQuery ?? sessionStorage.getItem(SCENARIO_KEY) ?? 'default';
    return (SCENARIOS as string[]).includes(raw) ? (raw as MockScenario) : 'default';
  } catch {
    return 'default';
  }
}

function sleep(ms: number): Promise<void> {
  return new Promise((resolve) => setTimeout(resolve, ms));
}

class MockApiError extends Error {
  readonly status: number;
  constructor(message: string, status: number) {
    super(message);
    this.name = 'ApiError';
    this.status = status;
  }
}

async function gate(): Promise<void> {
  const scenario = mockScenario();
  await sleep(scenario === 'slow' ? 1100 : 160);
  if (scenario === 'error') {
    throw new MockApiError('模拟故障：后端返回 500（?mock=error 关闭即可恢复）', 500);
  }
}

/* --------------------------------------------------------------- 数据集 */

interface MockUnit {
  id: string;
  course_id: string;
  title: string;
  slides: number;
  est_sec: number;
  ready: boolean;
  plan_source: string;
  status: T.UnitStatus;
  percent: number;
  last_slide: number;
  outline: T.OutlinePage[];
}

interface MockCourse {
  id: string;
  title: string;
  subject: string;
  source: T.CourseSource;
  updated_at: string;
  chapters: { title: string; units: MockUnit[] }[];
}

function page(index: number, title: string, preview: string, sec: number, kp: string[]): T.OutlinePage {
  return { index, title, units: [{ preview, est_sec: sec, kp }] };
}

function genericOutline(unit: Omit<MockUnit, 'outline'>): T.OutlinePage[] {
  const titles = ['导入', '概念', '例题', '对比', '小结', '练习', '拓展', '回顾', '应用', '课后', '复盘', '测验'];
  const pages: T.OutlinePage[] = [];
  for (let i = 0; i < Math.min(unit.slides, titles.length); i += 1) {
    pages.push(
      page(
        i,
        `第 ${i + 1} 页 · ${titles[i]}`,
        `${unit.title}：第 ${i + 1} 页的讲解要点会先说结论，再给出一个具体例子。`,
        45 + (i % 3) * 15,
        [unit.title.slice(0, 4), titles[i]],
      ),
    );
  }
  return pages;
}

const PHYS_1: MockUnit = {
  id: 'u_phys_1_1',
  course_id: 'c_phys_1',
  title: '质点与参考系',
  slides: 5,
  est_sec: 720,
  ready: true,
  plan_source: 'model',
  status: 'in_progress',
  percent: 40,
  last_slide: 1,
  outline: [
    page(0, '封面', '点明这一课要解决的两个问题：什么是质点，参考系为什么必须先定。', 40, ['质点', '参考系']),
    page(1, '本节目标', '三条目标：判断质点、选出参考系、用坐标描述位置。', 55, ['学习目标']),
    page(2, '质点与参考系', '质点不是小物体；地球公转能当质点、自转不能。参考系不同，描述可能不同。', 180, ['质点', '参考系', '理想化模型']),
    page(3, '位移与路程', '位移是矢量、路程是标量；绕操场一圈位移为零。', 200, ['位移', '路程', '矢量']),
    page(4, '小结', '三句话收口，并留一道画位移与路程的课后题。', 120, ['小结', '课后任务']),
  ],
};

const PHYS_2: MockUnit = {
  id: 'u_phys_1_2',
  course_id: 'c_phys_1',
  title: '位移与路程',
  slides: 6,
  est_sec: 810,
  ready: true,
  plan_source: 'rule',
  status: 'not_started',
  percent: 0,
  last_slide: 0,
  outline: [],
};

const PHYS_3: MockUnit = {
  id: 'u_phys_1_3',
  course_id: 'c_phys_1',
  title: '速度与加速度',
  slides: 7,
  est_sec: 960,
  ready: true,
  plan_source: 'model',
  status: 'completed',
  percent: 100,
  last_slide: 6,
  outline: [],
};

const PHYS_4: MockUnit = {
  id: 'u_phys_2_1',
  course_id: 'c_phys_1',
  title: '匀变速直线运动的规律',
  slides: 8,
  est_sec: 1080,
  ready: true,
  plan_source: 'model',
  status: 'not_started',
  percent: 0,
  last_slide: 0,
  outline: [],
};

const PHYS_5: MockUnit = {
  id: 'u_phys_2_2',
  course_id: 'c_phys_1',
  title: '自由落体运动',
  slides: 6,
  est_sec: 780,
  ready: false,
  plan_source: '',
  status: 'not_started',
  percent: 0,
  last_slide: 0,
  outline: [],
};

const ENG_1: MockUnit = {
  id: 'u_eng_1',
  course_id: 'c_english_u2',
  title: '核心词汇精讲',
  slides: 12,
  est_sec: 1500,
  ready: true,
  plan_source: 'model',
  status: 'in_progress',
  percent: 25,
  last_slide: 2,
  outline: [],
};

const ENG_2: MockUnit = {
  id: 'u_eng_2',
  course_id: 'c_english_u2',
  title: '课文精读',
  slides: 8,
  est_sec: 1020,
  ready: true,
  plan_source: 'model',
  status: 'not_started',
  percent: 0,
  last_slide: 0,
  outline: [],
};

const ENG_3: MockUnit = {
  id: 'u_eng_3',
  course_id: 'c_english_u2',
  title: '语法聚焦：现在完成时',
  slides: 5,
  est_sec: 600,
  ready: true,
  plan_source: 'rule',
  status: 'not_started',
  percent: 0,
  last_slide: 0,
  outline: [],
};

const ML_1: MockUnit = {
  id: 'u_ml_1',
  course_id: 'c_ml_1',
  title: '从数据到模型',
  slides: 9,
  est_sec: 1200,
  ready: true,
  plan_source: 'model',
  status: 'in_progress',
  percent: 72,
  last_slide: 6,
  outline: [],
};

const ML_2: MockUnit = {
  id: 'u_ml_2',
  course_id: 'c_ml_1',
  title: '最小二乘与损失函数',
  slides: 7,
  est_sec: 900,
  ready: true,
  plan_source: 'model',
  status: 'not_started',
  percent: 0,
  last_slide: 0,
  outline: [],
};

const ML_3: MockUnit = {
  id: 'u_ml_3',
  course_id: 'c_ml_1',
  title: '过拟合与正则化',
  slides: 6,
  est_sec: 840,
  ready: false,
  plan_source: '',
  status: 'not_started',
  percent: 0,
  last_slide: 0,
  outline: [],
};

const ALL_UNITS: MockUnit[] = [PHYS_1, PHYS_2, PHYS_3, PHYS_4, PHYS_5, ENG_1, ENG_2, ENG_3, ML_1, ML_2, ML_3];

for (const unit of ALL_UNITS) {
  if (!unit.outline.length) unit.outline = genericOutline(unit);
}

let COURSES: MockCourse[] = [
  {
    id: 'c_phys_1',
    title: '高一物理第一课 · 运动的描述',
    subject: '物理',
    source: 'run',
    updated_at: '2026-02-18T09:12:00Z',
    chapters: [
      { title: '第一章 运动的描述', units: [PHYS_1, PHYS_2, PHYS_3] },
      { title: '第二章 匀变速直线运动', units: [PHYS_4, PHYS_5] },
    ],
  },
  {
    id: 'c_english_u2',
    title: 'Unit 2 Sweet Home 单词解析',
    subject: '英语',
    source: 'import',
    updated_at: '2026-02-16T14:40:00Z',
    chapters: [
      { title: '第一章 词汇与语篇', units: [ENG_1, ENG_2] },
      { title: '第二章 语法', units: [ENG_3] },
    ],
  },
  {
    id: 'c_ml_1',
    title: '机器学习入门 · 线性回归',
    subject: '数学',
    source: 'run',
    updated_at: '2026-02-11T20:05:00Z',
    chapters: [
      { title: '第一章 从数据到模型', units: [ML_1, ML_2] },
      { title: '第二章 泛化', units: [ML_3] },
    ],
  },
];

const PROFILES: T.Profile[] = [
  { id: 'p_local', name: '本机学生', created_at: '2026-02-01T08:00:00Z' },
  { id: 'p_guest', name: '旁听', created_at: '2026-02-10T08:00:00Z' },
];
let activeProfileId = 'p_local';

interface MockSession {
  id: string;
  unit_id: string;
  profile_id: string;
  status: 'active' | 'paused' | 'ended';
  started_at: number;
  elapsed_sec: number;
  position: T.SessionPosition;
  qa_count: number;
  slides_seen: Set<number>;
  topics: string[];
}

const SESSIONS = new Map<string, MockSession>();

function findUnit(unitId: string): MockUnit {
  const unit = ALL_UNITS.find((u) => u.id === unitId);
  if (!unit) throw new MockApiError(`找不到单元 ${unitId}`, 404);
  return unit;
}

function findCourse(courseId: string): MockCourse {
  const course = COURSES.find((c) => c.id === courseId);
  if (!course) throw new MockApiError(`找不到课程 ${courseId}`, 404);
  return course;
}

function unitsOf(course: MockCourse): MockUnit[] {
  return course.chapters.flatMap((chapter) => chapter.units);
}

function courseSummary(course: MockCourse): T.CourseSummary {
  const units = unitsOf(course);
  const started = units.filter((u) => u.status !== 'not_started').length;
  const completed = units.filter((u) => u.status === 'completed').length;
  const percent = units.length
    ? Math.round(units.reduce((sum, u) => sum + u.percent, 0) / units.length)
    : 0;
  return {
    id: course.id,
    title: course.title,
    subject: course.subject,
    source: course.source,
    unit_count: units.length,
    unit_ready_count: units.filter((u) => u.ready).length,
    progress: { started, completed, percent },
    updated_at: course.updated_at,
  };
}

function sessionDetail(session: MockSession): T.SessionDetail {
  return {
    id: session.id,
    unit_id: session.unit_id,
    status: session.status,
    started_at: new Date(session.started_at).toISOString(),
    elapsed_sec: session.elapsed_sec,
    position: session.position,
    comprehension: null,
    qa_count: session.qa_count,
  };
}

function recapOf(session: MockSession): T.Recap {
  const unit = ALL_UNITS.find((u) => u.id === session.unit_id);
  const topics = session.topics.length
    ? session.topics
    : (unit?.outline.flatMap((p) => p.units.flatMap((u) => u.kp)).slice(0, 6) ?? []);
  return {
    elapsed_sec: Math.round(session.elapsed_sec),
    slides_covered: session.slides_seen.size,
    units_given: session.slides_seen.size,
    qa_count: session.qa_count,
    topics: Array.from(new Set(topics)).slice(0, 8),
  };
}

/** mock 课堂把进度回写到会话，结束课堂时 recap 才不是空的。 */
export function mockSessionTouch(sessionId: string, patch: { slide?: number; qa?: boolean; topic?: string }): void {
  const session = SESSIONS.get(sessionId);
  if (!session) return;
  if (typeof patch.slide === 'number') {
    session.slides_seen.add(patch.slide);
    session.position = { ...session.position, slide: patch.slide };
  }
  if (patch.qa) session.qa_count += 1;
  if (patch.topic) session.topics.push(patch.topic);
}

export const mockApi = {
  async health(): Promise<T.Health> {
    await gate();
    return {
      ok: true,
      version: '1.0.0-mock',
      courses: COURSES.length,
      engine: { provider: 'mock', model: 'mock-1', tts: 'mock-tts' },
    };
  },

  async getProfiles(): Promise<T.ProfilesResponse> {
    await gate();
    return { active_id: activeProfileId, items: PROFILES.map((p) => ({ ...p })) };
  },

  async createProfile(name: string): Promise<T.Profile> {
    await gate();
    const profile: T.Profile = { id: 'p_' + Date.now().toString(36), name, created_at: new Date().toISOString() };
    PROFILES.push(profile);
    activeProfileId = profile.id;
    return { ...profile };
  },

  async activateProfile(id: string): Promise<void> {
    await gate();
    if (!PROFILES.some((p) => p.id === id)) throw new MockApiError('档案不存在', 404);
    activeProfileId = id;
  },

  async deleteProfile(id: string): Promise<void> {
    await gate();
    const index = PROFILES.findIndex((p) => p.id === id);
    if (index >= 0 && PROFILES.length > 1) {
      PROFILES.splice(index, 1);
      if (activeProfileId === id) activeProfileId = PROFILES[0].id;
    }
  },

  async listCourses(): Promise<T.CourseSummary[]> {
    await gate();
    if (mockScenario() === 'empty') return [];
    return COURSES.map(courseSummary);
  },

  async scanCourses(): Promise<T.ScanResult> {
    await gate();
    const scenario = mockScenario();
    if (scenario === 'empty') {
      COURSES = [
        {
          id: 'c_scanned',
          title: '扫描到的课程 · 化学第一课',
          subject: '化学',
          source: 'run',
          updated_at: new Date().toISOString(),
          chapters: [
            {
              title: '第一章 物质的分类',
              units: [PHYS_1, PHYS_2].map((u, i) => ({
                ...u,
                id: 'u_scan_' + i,
                course_id: 'c_scanned',
                title: i === 0 ? '物质的分类' : '分散系与胶体',
                status: 'not_started',
                percent: 0,
              })),
            },
          ],
        },
      ];
      return { added: 1, updated: 0, total: 1 };
    }
    return { added: 0, updated: COURSES.length, total: COURSES.length };
  },

  async importCourse(file: File): Promise<T.ImportResult> {
    await gate();
    if (!/\.pptx$/i.test(file.name)) {
      throw new MockApiError('只支持 .pptx 文件，请换一个文件再试', 400);
    }
    const id = 'c_import_' + Date.now().toString(36);
    const slides = 8;
    COURSES.unshift({
      id,
      title: file.name.replace(/\.pptx$/i, ''),
      subject: '导入',
      source: 'import',
      updated_at: new Date().toISOString(),
      chapters: [
        {
          title: '导入课件',
          units: [
            {
              id: 'u_import_1',
              course_id: id,
              title: '整份课件',
              slides,
              est_sec: slides * 120,
              ready: true,
              plan_source: 'rule',
              status: 'not_started',
              percent: 0,
              last_slide: 0,
              outline: genericOutline({
                id: 'u_import_1',
                course_id: id,
                title: '整份课件',
                slides,
                est_sec: slides * 120,
                ready: true,
                plan_source: 'rule',
                status: 'not_started',
                percent: 0,
                last_slide: 0,
              }),
            },
          ],
        },
      ],
    });
    return { course_id: id, unit_id: 'u_import_1', slides };
  },

  async getCourse(id: string): Promise<T.CourseDetail> {
    await gate();
    if (mockScenario() === 'empty') {
      return { id, title: '空课程', subject: '', source: 'import', chapters: [] };
    }
    const course = findCourse(id);
    return {
      id: course.id,
      title: course.title,
      subject: course.subject,
      source: course.source,
      chapters: course.chapters.map((chapter) => ({
        title: chapter.title,
        units: chapter.units.map((u) => ({
          id: u.id,
          title: u.title,
          slides: u.slides,
          est_sec: u.est_sec,
          ready: u.ready,
          plan_source: u.plan_source,
          progress: { percent: u.percent, last_slide: u.last_slide, status: u.status },
        })),
      })),
    };
  },

  async deleteCourse(id: string): Promise<void> {
    await gate();
    COURSES = COURSES.filter((c) => c.id !== id);
  },

  async getUnit(id: string): Promise<T.UnitDetail> {
    await gate();
    const unit = findUnit(id);
    return {
      id: unit.id,
      course_id: unit.course_id,
      title: unit.title,
      slides: unit.slides,
      est_sec: unit.est_sec,
      ready: unit.ready,
      plan_source: unit.plan_source,
      progress: { percent: unit.percent, last_slide: unit.last_slide, status: unit.status },
    };
  },

  async getUnitOutline(id: string): Promise<T.Outline> {
    await gate();
    const unit = findUnit(id);
    const empty = mockScenario() === 'empty' || mockScenario() === 'unprepared' || !unit.ready;
    const pages = empty ? [] : unit.outline;
    return {
      totals: {
        slides: unit.slides,
        units: pages.reduce((sum, p) => sum + p.units.length, 0),
        est_sec: pages.reduce((sum, p) => sum + p.units.reduce((s, u) => s + u.est_sec, 0), 0),
      },
      pages,
    };
  },

  async prepareUnit(id: string): Promise<T.PrepareResult> {
    await gate();
    const unit = findUnit(id);
    unit.ready = true;
    unit.plan_source = 'rule';
    if (!unit.outline.length) unit.outline = genericOutline(unit);
    const degraded = mockScenario() === 'unprepared' || mockScenario() === 'default';
    return {
      ok: true,
      deck_ready: true,
      plan_ready: true,
      plan_source: 'rule',
      degraded,
      warnings: degraded ? ['未配置模型，已用规则兜底备课（内容可用但不够细）'] : [],
    };
  },

  async createSession(input: { unit_id: string; profile_id: string; start_slide?: number }): Promise<T.SessionCreated> {
    await gate();
    const unit = findUnit(input.unit_id);
    const existing = Array.from(SESSIONS.values()).find(
      (s) => s.unit_id === input.unit_id && s.status !== 'ended',
    );
    if (existing) {
      return { session_id: existing.id, resumed: true, position: existing.position };
    }
    const id = 's_' + Date.now().toString(36);
    const startSlide = typeof input.start_slide === 'number' ? input.start_slide : unit.last_slide;
    const session: MockSession = {
      id,
      unit_id: input.unit_id,
      profile_id: input.profile_id,
      status: 'active',
      started_at: Date.now(),
      elapsed_sec: 0,
      position: { slide: startSlide, unit_index: 0 },
      qa_count: 0,
      slides_seen: new Set([startSlide]),
      topics: [],
    };
    SESSIONS.set(id, session);
    return { session_id: id, resumed: false, position: session.position };
  },

  async getSession(id: string): Promise<T.SessionDetail> {
    await gate();
    const session = SESSIONS.get(id);
    if (!session) {
      throw new MockApiError(`找不到会话 ${id}（mock 重启后会话会丢失）`, 404);
    }
    session.elapsed_sec = (Date.now() - session.started_at) / 1000;
    return sessionDetail(session);
  },

  async endSession(id: string): Promise<T.EndSessionResult> {
    await gate();
    const session = SESSIONS.get(id);
    if (!session) throw new MockApiError('会话不存在或已经结束', 404);
    session.elapsed_sec = (Date.now() - session.started_at) / 1000;
    session.status = 'ended';
    return { ok: true, recap: recapOf(session) };
  },

  async getSessionRecap(id: string): Promise<T.Recap> {
    await gate();
    const session = SESSIONS.get(id);
    if (!session) throw new MockApiError('会话记录已丢失（mock 重启会清空会话）', 404);
    session.elapsed_sec = session.elapsed_sec || (Date.now() - session.started_at) / 1000;
    return recapOf(session);
  },
};
