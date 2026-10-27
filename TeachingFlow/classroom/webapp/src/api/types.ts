/** §4 HTTP 契约 v1 的类型化定义（唯一事实源：docs/正式版设计.md）。 */

export type CourseSource = 'run' | 'import';
export type UnitStatus = 'not_started' | 'in_progress' | 'completed' | 'paused';

export interface Profile {
  id: string;
  name: string;
  created_at: string;
}

export interface ProfilesResponse {
  active_id: string;
  items: Profile[];
}

export interface CourseProgress {
  started: number;
  completed: number;
  percent: number;
}

export interface CourseSummary {
  id: string;
  title: string;
  subject: string;
  source: CourseSource;
  unit_count: number;
  unit_ready_count: number;
  progress: CourseProgress;
  updated_at: string;
}

export interface ScanResult {
  added: number;
  updated: number;
  total: number;
}

export interface ImportResult {
  course_id: string;
  unit_id: string;
  slides: number;
}

export interface UnitProgress {
  percent: number;
  last_slide: number;
  status: UnitStatus | string;
}

export interface CourseUnit {
  id: string;
  title: string;
  slides: number;
  est_sec: number;
  ready: boolean;
  plan_source: string;
  progress: UnitProgress;
}

export interface CourseChapter {
  title: string;
  units: CourseUnit[];
}

export interface CourseDetail {
  id: string;
  title: string;
  subject: string;
  source: CourseSource;
  chapters: CourseChapter[];
}

export interface UnitDetail {
  id: string;
  course_id: string;
  title: string;
  slides: number;
  est_sec: number;
  ready: boolean;
  plan_source: string;
  progress: UnitProgress;
}

export interface OutlineUnit {
  preview: string;
  est_sec: number;
  kp: string[];
}

export interface OutlinePage {
  index: number;
  title: string;
  units: OutlineUnit[];
}

export interface Outline {
  totals: { slides: number; units: number; est_sec: number };
  pages: OutlinePage[];
}

export interface PrepareResult {
  ok: boolean;
  deck_ready: boolean;
  plan_ready: boolean;
  plan_source: string;
  degraded: boolean;
  warnings: string[];
}

export interface SessionPosition {
  slide: number;
  unit_index: number;
}

export interface SessionCreated {
  session_id: string;
  resumed: boolean;
  position: SessionPosition;
}

export interface SessionDetail {
  id: string;
  unit_id: string;
  status: 'active' | 'paused' | 'ended' | string;
  started_at: string;
  elapsed_sec: number;
  position: SessionPosition;
  comprehension: number | null;
  qa_count: number;
}

export interface Recap {
  elapsed_sec: number;
  slides_covered: number;
  units_given: number;
  qa_count: number;
  topics: string[];
}

export interface EndSessionResult {
  ok: boolean;
  recap: Recap;
}

export interface Health {
  ok: boolean;
  version: string;
  courses: number;
  engine: { provider: string; model: string; tts: string };
}
